from __future__ import annotations

import os
import stat
from pathlib import Path  # noqa: TC003

import pytest

from dr_store.localfs import _fs as fs_module
from dr_store.localfs import (
    ensure_private_directory,
    open_private_directory,
)


def _identity(fd: int) -> tuple[int, int]:
    status = os.fstat(fd)
    return status.st_dev, status.st_ino


def test_nested_creation_fsyncs_each_receiving_parent_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "new" / "nested" / "run.partial"
    events: list[tuple[str, tuple[int, int], str | None]] = []
    real_mkdir = os.mkdir
    real_fsync = os.fsync

    def observe_mkdir(
        path: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        assert dir_fd is not None
        events.append(("mkdir", _identity(dir_fd), path))
        real_mkdir(path, mode, dir_fd=dir_fd)

    def observe_fsync(fd: int) -> None:
        events.append(("fsync", _identity(fd), None))
        real_fsync(fd)

    monkeypatch.setattr(fs_module.os, "mkdir", observe_mkdir)
    monkeypatch.setattr(fs_module.os, "fsync", observe_fsync)

    ensure_private_directory(target)

    expected = [
        ("mkdir", (tmp_path.stat().st_dev, tmp_path.stat().st_ino), "new"),
        (
            "fsync",
            (
                (tmp_path / "new").stat().st_dev,
                (tmp_path / "new").stat().st_ino,
            ),
            None,
        ),
        ("fsync", (tmp_path.stat().st_dev, tmp_path.stat().st_ino), None),
        (
            "mkdir",
            (
                (tmp_path / "new").stat().st_dev,
                (tmp_path / "new").stat().st_ino,
            ),
            "nested",
        ),
        (
            "fsync",
            (
                (tmp_path / "new" / "nested").stat().st_dev,
                (tmp_path / "new" / "nested").stat().st_ino,
            ),
            None,
        ),
        (
            "fsync",
            (
                (tmp_path / "new").stat().st_dev,
                (tmp_path / "new").stat().st_ino,
            ),
            None,
        ),
        (
            "mkdir",
            (
                (tmp_path / "new" / "nested").stat().st_dev,
                (tmp_path / "new" / "nested").stat().st_ino,
            ),
            "run.partial",
        ),
        (
            "fsync",
            (target.stat().st_dev, target.stat().st_ino),
            None,
        ),
        (
            "fsync",
            (
                (tmp_path / "new" / "nested").stat().st_dev,
                (tmp_path / "new" / "nested").stat().st_ino,
            ),
            None,
        ),
    ]
    assert events == expected
    for directory in [
        tmp_path / "new",
        tmp_path / "new" / "nested",
        target,
    ]:
        status = directory.stat()
        assert stat.S_IMODE(status.st_mode) == 0o700
        assert status.st_uid == os.geteuid()


def test_existing_ancestor_is_not_repaired_or_recreated(
    tmp_path: Path,
) -> None:
    ancestor = tmp_path / "existing"
    ancestor.mkdir(mode=0o755)
    ancestor.chmod(0o755)

    ensure_private_directory(ancestor / "run.partial")

    assert stat.S_IMODE(ancestor.stat().st_mode) == 0o755
    assert stat.S_IMODE((ancestor / "run.partial").stat().st_mode) == 0o700


def test_all_masking_umask_succeeds_and_retry_is_clean(tmp_path: Path) -> None:
    target = tmp_path / "masked" / "nested" / "run.partial"
    previous_umask = os.umask(0o777)
    try:
        ensure_private_directory(target)
        ensure_private_directory(target)
        observed_umask = os.umask(0o777)
        assert observed_umask == 0o777
    finally:
        os.umask(previous_umask)

    for directory in [target.parent.parent, target.parent, target]:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert directory.stat().st_uid == os.geteuid()


def test_replace_is_same_directory_and_complete(tmp_path: Path) -> None:
    with open_private_directory(tmp_path / "priv") as directory:
        source_fd = directory.open_regular(
            "tmp",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        try:
            os.write(source_fd, b"new-body")
        finally:
            os.close(source_fd)
        dest_fd = directory.open_regular(
            "dest",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        try:
            os.write(dest_fd, b"old-body")
        finally:
            os.close(dest_fd)

        directory.replace("tmp", "dest")

        names = directory.list_names()
        assert "tmp" not in names
        assert "dest" in names
        assert directory.stat("dest").st_size == len(b"new-body")
        assert (tmp_path / "priv" / "dest").read_bytes() == b"new-body"


def test_unlink_removes_only_the_named_child(tmp_path: Path) -> None:
    with open_private_directory(tmp_path / "priv") as directory:
        fd = directory.open_regular("keep", os.O_WRONLY | os.O_CREAT)
        os.close(fd)
        fd = directory.open_regular("drop", os.O_WRONLY | os.O_CREAT)
        os.close(fd)
        directory.unlink("drop")
        assert directory.list_names() == ["keep"]


def test_creat_retries_filenotfounderror_four_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = open_private_directory(tmp_path / "priv")
    real_open = os.open
    remaining = {"n": 3}

    def flaky(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if flags & os.O_CREAT and remaining["n"]:
            remaining["n"] -= 1
            raise FileNotFoundError(path)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(fs_module.os, "open", flaky)
    try:
        fd = directory.open_regular("child", os.O_WRONLY | os.O_CREAT)
        os.close(fd)
        assert remaining["n"] == 0
    finally:
        directory.close()


def test_creat_exhausts_filenotfounderror_after_four_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = open_private_directory(tmp_path / "priv")
    attempts = {"n": 0}

    def always_missing(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if flags & os.O_CREAT:
            attempts["n"] += 1
            raise FileNotFoundError(path)
        if dir_fd is None:
            return os.open(path, flags, mode)
        return os.open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(fs_module.os, "open", always_missing)
    try:
        with pytest.raises(FileNotFoundError):
            directory.open_regular("child", os.O_WRONLY | os.O_CREAT)
        assert attempts["n"] == 4
    finally:
        directory.close()


def test_open_without_creat_does_not_retry_filenotfound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = open_private_directory(tmp_path / "priv")
    attempts = {"n": 0}

    def missing(*_args: object, **_kwargs: object) -> int:
        attempts["n"] += 1
        raise FileNotFoundError

    monkeypatch.setattr(fs_module.os, "open", missing)
    try:
        with pytest.raises(FileNotFoundError):
            directory.open_regular("missing", os.O_RDONLY)
        assert attempts["n"] == 1
    finally:
        directory.close()
