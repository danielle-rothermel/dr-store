from __future__ import annotations

import errno
import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from dr_store.localfs.errors import (
    LocalFsError,
    LockNotHeldError,
    PrivateDirectoryClosedError,
    PrivatePathReason,
    PrivatePathViolationError,
)

_REQUIRED_OPEN_FLAGS = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700


def _require_posix_support() -> None:
    missing = [
        flag
        for flag in _REQUIRED_OPEN_FLAGS
        if not isinstance(getattr(os, flag, None), int)
    ]
    if not _OPEN_SUPPORTS_DIR_FD:
        missing.append("os.open(dir_fd=...)")
    if not hasattr(fcntl, "flock"):
        missing.append("fcntl.flock")
    if missing:
        raise OSError(
            errno.ENOTSUP,
            "private directories require a POSIX local filesystem: "
            + ", ".join(missing),
        )


def _descriptor_flags(flags: int) -> int:
    return flags | os.O_CLOEXEC | os.O_NOFOLLOW


def _directory_flags() -> int:
    return _descriptor_flags(os.O_RDONLY | os.O_DIRECTORY)


def _raise_if_symlink(
    exc: OSError,
    path: Path,
    *,
    name: str | None = None,
    dir_fd: int | None = None,
) -> None:
    if exc.errno == errno.ELOOP:
        raise PrivatePathViolationError(
            path=path,
            reason=PrivatePathReason.SYMLINKED,
        ) from exc
    # macOS reports O_NOFOLLOW|O_DIRECTORY on a symlink as ENOTDIR.
    if exc.errno != errno.ENOTDIR:
        return
    try:
        status = (
            os.lstat(name, dir_fd=dir_fd)
            if name is not None and dir_fd is not None
            else os.lstat(path)
        )
    except OSError:
        return
    if stat.S_ISLNK(status.st_mode):
        raise PrivatePathViolationError(
            path=path,
            reason=PrivatePathReason.SYMLINKED,
        ) from exc


def _validate_owned_descriptor(
    fd: int,
    *,
    path: Path,
    expected_type: int,
) -> os.stat_result:
    status = os.fstat(fd)
    if stat.S_IFMT(status.st_mode) != expected_type:
        raise PrivatePathViolationError(
            path=path,
            reason=PrivatePathReason.WRONG_TYPE,
        )
    if status.st_uid != os.geteuid():
        raise PrivatePathViolationError(
            path=path,
            reason=PrivatePathReason.NOT_OWNED,
        )
    if expected_type == stat.S_IFREG and status.st_nlink != 1:
        raise PrivatePathViolationError(
            path=path,
            reason=PrivatePathReason.HARD_LINKED,
        )
    return status


def _validate_component_name(name: str) -> None:
    if name in {"", ".", ".."} or Path(name).name != name:
        raise ValueError(f"managed path component must be one name: {name!r}")


def _open_or_create_directory(
    parent_fd: int,
    name: str,
    *,
    path: Path,
    create: bool,
) -> tuple[int, bool, bool]:
    try:
        fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        created = False
        try:
            os.mkdir(name, mode=_PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        if created:
            os.chmod(
                name,
                _PRIVATE_DIRECTORY_MODE,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        try:
            return (
                os.open(name, _directory_flags(), dir_fd=parent_fd),
                created,
                True,
            )
        except OSError as exc:
            _raise_if_symlink(exc, path, name=name, dir_fd=parent_fd)
            raise
    except OSError as exc:
        _raise_if_symlink(exc, path, name=name, dir_fd=parent_fd)
        raise
    else:
        return fd, False, False


def _finish_private_regular_fd(
    fd: int,
    *,
    path: Path,
    truncate: bool,
) -> int:
    try:
        status = _validate_owned_descriptor(
            fd,
            path=path,
            expected_type=stat.S_IFREG,
        )
        if stat.S_IMODE(status.st_mode) != _PRIVATE_FILE_MODE:
            os.fchmod(fd, _PRIVATE_FILE_MODE)
        # Defer O_TRUNC until after policy checks so a refused path is
        # not emptied by the open itself.
        if truncate:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_private_regular_at(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
    flags: int,
    mode: int = 0o600,
) -> int:
    _validate_component_name(name)
    # O_CREAT can race with a sibling unlink; retry a bounded number of
    # times rather than treating a transient ENOENT as a hard failure.
    attempts = 4 if flags & os.O_CREAT else 1
    open_flags = flags & ~os.O_TRUNC
    truncate = bool(flags & os.O_TRUNC)
    fd: int | None = None
    for attempt in range(attempts):
        try:
            fd = os.open(
                name,
                _descriptor_flags(open_flags | os.O_NONBLOCK),
                mode,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            if attempt + 1 == attempts:
                raise
            continue
        except OSError as exc:
            _raise_if_symlink(
                exc,
                display_path,
                name=name,
                dir_fd=directory_fd,
            )
            raise
        break
    if fd is None:
        raise FileNotFoundError(display_path)
    return _finish_private_regular_fd(
        fd,
        path=display_path,
        truncate=truncate,
    )


@dataclass(slots=True)
class PrivateDirectory:
    path: Path
    fd: int
    _closed: bool = False

    def __enter__(self) -> Self:
        if self._closed:
            raise PrivateDirectoryClosedError
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self.fd)

    def _active_fd(self) -> int:
        if self._closed:
            raise PrivateDirectoryClosedError
        return self.fd

    def open_regular(
        self,
        name: str,
        flags: int,
        mode: int = 0o600,
    ) -> int:
        return _open_private_regular_at(
            self._active_fd(),
            name,
            display_path=self.path / name,
            flags=flags,
            mode=mode,
        )

    def open_child(
        self,
        name: str,
        *,
        create: bool = True,
    ) -> Self:
        _validate_component_name(name)
        parent_fd = self._active_fd()
        child_path = self.path / name
        child_fd, created, _was_missing = _open_or_create_directory(
            parent_fd,
            name,
            path=child_path,
            create=create,
        )
        try:
            status = _validate_owned_descriptor(
                child_fd,
                path=child_path,
                expected_type=stat.S_IFDIR,
            )
            if stat.S_IMODE(status.st_mode) != _PRIVATE_DIRECTORY_MODE:
                os.fchmod(child_fd, _PRIVATE_DIRECTORY_MODE)
                os.fsync(child_fd)
            if created:
                os.fsync(child_fd)
                os.fsync(parent_fd)
        except BaseException:
            os.close(child_fd)
            raise
        return type(self)(path=child_path, fd=child_fd)

    def stat(self, name: str) -> os.stat_result:
        _validate_component_name(name)
        return os.stat(
            name,
            dir_fd=self._active_fd(),
            follow_symlinks=False,
        )

    def list_names(self) -> list[str]:
        return os.listdir(self._active_fd())  # noqa: PTH208

    def replace(self, source: str, destination: str) -> None:
        _validate_component_name(source)
        _validate_component_name(destination)
        fd = self._active_fd()
        os.replace(
            source,
            destination,
            src_dir_fd=fd,
            dst_dir_fd=fd,
        )

    def unlink(self, name: str) -> None:
        _validate_component_name(name)
        os.unlink(name, dir_fd=self._active_fd())

    def fsync(self) -> None:
        os.fsync(self._active_fd())


def open_private_directory(
    path: Path,
    *,
    create: bool = True,
) -> PrivateDirectory:
    _require_posix_support()
    # abspath collapses ``..`` without following symlinks; resolve() would
    # rewrite attacker-controlled intermediates. Platform aliases such as
    # macOS ``/var`` stay lexical and fail the no-follow walk.
    absolute = Path(os.path.abspath(path))  # noqa: PTH100
    if len(absolute.parts) == 1:
        raise PrivatePathViolationError(
            path=absolute,
            reason=PrivatePathReason.FILESYSTEM_ROOT,
        )
    current_path = Path(absolute.anchor)
    try:
        current_fd = os.open(current_path, _directory_flags())
    except OSError as exc:
        _raise_if_symlink(exc, current_path)
        raise
    try:
        private_chain = False
        for index, component in enumerate(absolute.parts[1:]):
            next_path = current_path / component
            next_fd, created, was_missing = _open_or_create_directory(
                current_fd,
                component,
                path=next_path,
                create=create,
            )
            if was_missing:
                private_chain = True
            try:
                is_leaf = index == len(absolute.parts) - 2
                if private_chain or is_leaf:
                    status = _validate_owned_descriptor(
                        next_fd,
                        path=next_path,
                        expected_type=stat.S_IFDIR,
                    )
                    if stat.S_IMODE(status.st_mode) != _PRIVATE_DIRECTORY_MODE:
                        os.fchmod(next_fd, _PRIVATE_DIRECTORY_MODE)
                        os.fsync(next_fd)
                if created:
                    os.fsync(next_fd)
                    os.fsync(current_fd)
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
            current_path = next_path
    except BaseException:
        os.close(current_fd)
        raise
    return PrivateDirectory(path=absolute, fd=current_fd)


def ensure_private_directory(path: Path) -> None:
    with open_private_directory(path):
        pass


def open_private_regular_file(
    path: Path,
    flags: int,
    mode: int = 0o600,
) -> int:
    _require_posix_support()
    open_flags = flags & ~os.O_TRUNC
    truncate = bool(flags & os.O_TRUNC)
    try:
        fd = os.open(
            path,
            _descriptor_flags(open_flags | os.O_NONBLOCK),
            mode,
        )
    except OSError as exc:
        _raise_if_symlink(exc, path)
        raise
    return _finish_private_regular_fd(fd, path=path, truncate=truncate)


class FileLock:
    def __init__(self, path: Path, *, shared: bool = False) -> None:
        self.path = path
        self.shared = shared
        self._fd: int | None = None
        self._directory: PrivateDirectory | None = None

    @property
    def directory(self) -> PrivateDirectory:
        directory = self._directory
        if directory is None:
            raise LockNotHeldError
        return directory

    def __enter__(self) -> Self:
        if self._fd is not None:
            raise LocalFsError("file lock is already held")
        directory = open_private_directory(self.path.parent)
        fd: int | None = None
        try:
            fd = directory.open_regular(
                self.path.name,
                os.O_RDWR | os.O_CREAT,
            )
            operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
            fcntl.flock(fd, operation)
        except BaseException:
            if fd is not None:
                os.close(fd)
            directory.close()
            raise
        self._fd = fd
        self._directory = directory
        return self

    def __exit__(self, *args: object) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        directory = self._directory
        self._directory = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(fd)
            finally:
                if directory is not None:
                    directory.close()


def fsync_file(fd: int) -> None:
    os.fsync(fd)


def fsync_parent_directory(path: Path) -> None:
    _require_posix_support()
    try:
        fd = os.open(path.parent, _directory_flags())
    except OSError as exc:
        _raise_if_symlink(exc, path.parent)
        raise
    try:
        _validate_owned_descriptor(
            fd,
            path=path.parent,
            expected_type=stat.S_IFDIR,
        )
        os.fsync(fd)
    finally:
        os.close(fd)
