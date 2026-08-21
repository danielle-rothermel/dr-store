from __future__ import annotations

import errno
import os
import shutil
import socket
import stat
import tempfile
from pathlib import Path

import pytest

from dr_store.localfs import (
    PrivatePathReason,
    PrivatePathViolationError,
    ensure_private_directory,
    fsync_parent_directory,
    open_private_directory,
    open_private_regular_file,
)
from dr_store.localfs import _fs as fs_module


def test_symlink_in_path_is_refused(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    external.chmod(0o755)
    link = tmp_path / "linked"
    link.symlink_to(external, target_is_directory=True)

    with pytest.raises(PrivatePathViolationError) as caught:
        ensure_private_directory(link / "run.partial")

    assert caught.value.reason is PrivatePathReason.SYMLINKED
    assert not (external / "run.partial").exists()
    assert stat.S_IMODE(external.stat().st_mode) == 0o755


def test_symlink_regular_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    link = tmp_path / "alias"
    link.symlink_to(target)

    with pytest.raises(PrivatePathViolationError) as caught:
        open_private_regular_file(link, os.O_RDONLY)

    assert caught.value.reason is PrivatePathReason.SYMLINKED


def test_foreign_owned_component_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "owned"
    target.mkdir()
    real_euid = os.geteuid()
    monkeypatch.setattr(fs_module.os, "geteuid", lambda: real_euid + 1)

    with pytest.raises(PrivatePathViolationError) as caught:
        ensure_private_directory(target)

    assert caught.value.reason is PrivatePathReason.NOT_OWNED
    assert caught.value.path == target


def test_hard_linked_regular_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"payload")
    os.link(path, tmp_path / "other")

    with pytest.raises(PrivatePathViolationError) as caught:
        open_private_regular_file(path, os.O_RDONLY)

    assert caught.value.reason is PrivatePathReason.HARD_LINKED


def test_trunc_open_does_not_empty_a_refused_file(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"payload")
    os.link(path, tmp_path / "other")

    with pytest.raises(PrivatePathViolationError) as caught:
        open_private_regular_file(path, os.O_RDWR | os.O_TRUNC)

    assert caught.value.reason is PrivatePathReason.HARD_LINKED
    assert path.read_bytes() == b"payload"


def test_trunc_open_empties_an_accepted_file(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"payload")

    fd = open_private_regular_file(path, os.O_RDWR | os.O_TRUNC)
    try:
        assert os.fstat(fd).st_size == 0
    finally:
        os.close(fd)
    assert path.read_bytes() == b""


def test_directory_trunc_open_does_not_empty_a_refused_child(
    tmp_path: Path,
) -> None:
    with open_private_directory(tmp_path / "priv") as directory:
        fd = directory.open_regular("file", os.O_WRONLY | os.O_CREAT)
        try:
            os.write(fd, b"payload")
        finally:
            os.close(fd)
        os.link(tmp_path / "priv" / "file", tmp_path / "priv" / "other")
        with pytest.raises(PrivatePathViolationError) as caught:
            directory.open_regular("file", os.O_RDWR | os.O_TRUNC)
        assert caught.value.reason is PrivatePathReason.HARD_LINKED
        assert directory.stat("file").st_size == len(b"payload")


def test_wrong_regular_file_mode_is_repaired(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"payload")
    path.chmod(0o644)

    fd = open_private_regular_file(path, os.O_RDONLY)
    try:
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
    finally:
        os.close(fd)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_wrong_directory_mode_is_repaired_on_leaf(tmp_path: Path) -> None:
    target = tmp_path / "leaf"
    target.mkdir(mode=0o755)
    target.chmod(0o755)

    ensure_private_directory(target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_regular_file_in_directory_path_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"payload")

    with pytest.raises(PrivatePathViolationError) as caught:
        ensure_private_directory(path / "child")

    assert caught.value.reason is PrivatePathReason.WRONG_TYPE
    assert not (path / "child").exists()


def test_intermediate_symlink_refuses_regular_file_open(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "file").write_bytes(b"payload")
    link = tmp_path / "linked"
    link.symlink_to(external, target_is_directory=True)

    with pytest.raises(PrivatePathViolationError) as caught:
        open_private_regular_file(link / "file", os.O_RDONLY)

    assert caught.value.reason is PrivatePathReason.SYMLINKED


def test_intermediate_symlink_refuses_parent_fsync(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "file").write_bytes(b"payload")
    link = tmp_path / "linked"
    link.symlink_to(external, target_is_directory=True)

    with pytest.raises(PrivatePathViolationError) as caught:
        fsync_parent_directory(link / "file")

    assert caught.value.reason is PrivatePathReason.SYMLINKED


def test_directory_opened_as_regular_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "dir"
    path.mkdir()

    with pytest.raises(PrivatePathViolationError) as caught:
        open_private_regular_file(path, os.O_RDONLY)

    assert caught.value.reason is PrivatePathReason.WRONG_TYPE


def test_writable_directory_open_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "dir"
    path.mkdir()

    with pytest.raises(PrivatePathViolationError) as caught:
        open_private_regular_file(path, os.O_WRONLY)

    assert caught.value.reason is PrivatePathReason.WRONG_TYPE


def test_unix_socket_open_is_refused() -> None:
    # Darwin AF_UNIX bind rejects the long pytest tmp_path.
    directory = Path(os.path.realpath(tempfile.mkdtemp(prefix="drs-s-")))
    path = directory / "s"
    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        bound.bind(os.fspath(path))
        with pytest.raises(PrivatePathViolationError) as caught:
            open_private_regular_file(path, os.O_RDONLY)
        assert caught.value.reason is PrivatePathReason.WRONG_TYPE
    finally:
        bound.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_fifo_read_open_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path)

    with pytest.raises(PrivatePathViolationError) as caught:
        open_private_regular_file(path, os.O_RDONLY)

    assert caught.value.reason is PrivatePathReason.WRONG_TYPE


def test_fifo_write_open_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path)

    with pytest.raises(PrivatePathViolationError) as caught:
        open_private_regular_file(path, os.O_WRONLY)

    assert caught.value.reason is PrivatePathReason.WRONG_TYPE


def test_filesystem_root_is_refused() -> None:
    with pytest.raises(PrivatePathViolationError) as caught:
        ensure_private_directory(Path("/"))

    assert caught.value.reason is PrivatePathReason.FILESYSTEM_ROOT


def test_missing_posix_flag_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(fs_module.os, "O_NOFOLLOW")

    with pytest.raises(OSError, match="POSIX local filesystem") as caught:
        open_private_directory(tmp_path / "priv")

    assert caught.value.errno == errno.ENOTSUP
