from __future__ import annotations

import os
from pathlib import Path

import pytest

from dr_store import localfs
from dr_store.localfs import (
    FileLock,
    LocalFsError,
    LockNotHeldError,
    PrivateDirectoryClosedError,
    open_private_directory,
)


def test_public_surface_is_exact() -> None:
    assert set(localfs.__all__) == {
        "FileLock",
        "LocalFsError",
        "LockNotHeldError",
        "PrivateDirectory",
        "PrivateDirectoryClosedError",
        "PrivatePathReason",
        "PrivatePathViolationError",
        "ensure_private_directory",
        "fsync_file",
        "fsync_parent_directory",
        "open_private_directory",
        "open_private_regular_file",
    }


def test_directory_before_enter_raises(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "name.lock")
    with pytest.raises(LockNotHeldError):
        _ = lock.directory


def test_directory_after_exit_raises(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "name.lock")
    with lock:
        assert lock.directory.path == Path(os.path.abspath(tmp_path))  # noqa: PTH100
    with pytest.raises(LockNotHeldError):
        _ = lock.directory


def test_file_lock_exit_is_idempotent(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "name.lock")
    with lock:
        pass
    lock.__exit__(None, None, None)


def test_reentry_while_held_is_refused(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "name.lock")
    with lock:
        with pytest.raises(LocalFsError, match="already held"):
            lock.__enter__()
        assert lock.directory.path == Path(os.path.abspath(tmp_path))  # noqa: PTH100


def test_closed_private_directory_operations_raise(tmp_path: Path) -> None:
    directory = open_private_directory(tmp_path / "priv")
    directory.close()
    directory.close()
    with pytest.raises(PrivateDirectoryClosedError):
        directory.list_names()
    with pytest.raises(PrivateDirectoryClosedError):
        directory.stat("missing")
    with pytest.raises(PrivateDirectoryClosedError):
        directory.fsync()
    with pytest.raises(PrivateDirectoryClosedError):
        directory.unlink("missing")
    with pytest.raises(PrivateDirectoryClosedError):
        directory.replace("a", "b")
    with pytest.raises(PrivateDirectoryClosedError):
        directory.__enter__()


def test_held_directory_handle_is_closed_after_lock_exit(
    tmp_path: Path,
) -> None:
    lock = FileLock(tmp_path / "name.lock")
    with lock:
        held = lock.directory
    with pytest.raises(PrivateDirectoryClosedError):
        held.list_names()


def test_bad_component_name_is_value_error(tmp_path: Path) -> None:
    with open_private_directory(tmp_path / "priv") as directory:
        with pytest.raises(ValueError, match="one name"):
            directory.stat("../escape")
        with pytest.raises(ValueError, match="one name"):
            directory.stat("")
        with pytest.raises(ValueError, match="one name"):
            directory.stat(".")
