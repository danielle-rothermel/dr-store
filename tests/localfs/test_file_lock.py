from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from dr_store.localfs import FileLock, PrivateDirectory

if TYPE_CHECKING:
    import pytest

WATCHDOG_SECONDS = 15


def test_lock_file_is_private_and_directory_is_parent(tmp_path: Path) -> None:
    lock_path = tmp_path / "storage.lock"
    with FileLock(lock_path) as lock:
        assert lock.directory.path == Path(os.path.abspath(tmp_path))  # noqa: PTH100
        status = lock_path.stat()
        assert stat.S_ISREG(status.st_mode)
        assert stat.S_IMODE(status.st_mode) == 0o600
        assert status.st_uid == os.geteuid()
        assert status.st_nlink == 1


def test_lock_open_remains_bound_to_verified_parent_after_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir(mode=0o700)
    relocated = tmp_path / "relocated"
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    lock_path = managed / "storage.lock"
    real_open = PrivateDirectory.open_regular
    substituted = False

    def substitute_before_open(
        directory: PrivateDirectory,
        name: str,
        flags: int,
        mode: int = 0o600,
    ) -> int:
        nonlocal substituted
        if not substituted and directory.path == managed:
            substituted = True
            managed.rename(relocated)
            managed.symlink_to(external, target_is_directory=True)
        return real_open(directory, name, flags, mode)

    monkeypatch.setattr(
        PrivateDirectory,
        "open_regular",
        substitute_before_open,
    )
    with FileLock(lock_path):
        pass

    assert (relocated / "storage.lock").is_file()
    assert not (external / "storage.lock").exists()


def test_two_exclusive_locks_in_one_process_exclude(tmp_path: Path) -> None:
    lock_path = tmp_path / "same.lock"
    events: list[str] = []
    held = threading.Event()
    peer_trying = threading.Event()

    def holder() -> None:
        with FileLock(lock_path):
            events.append("holder-enter")
            held.set()
            assert peer_trying.wait(WATCHDOG_SECONDS)
            events.append("holder-exit")

    def waiter() -> None:
        assert held.wait(WATCHDOG_SECONDS)
        peer_trying.set()
        with FileLock(lock_path):
            events.append("waiter-enter")
            events.append("waiter-exit")

    threads = [
        threading.Thread(target=holder),
        threading.Thread(target=waiter),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WATCHDOG_SECONDS)
        assert not thread.is_alive()
    assert events == [
        "holder-enter",
        "holder-exit",
        "waiter-enter",
        "waiter-exit",
    ]


def test_shared_readers_overlap_in_one_process(tmp_path: Path) -> None:
    lock_path = tmp_path / "shared.lock"
    first_inside = threading.Event()
    second_inside = threading.Event()

    def first_reader() -> None:
        with FileLock(lock_path, shared=True):
            first_inside.set()
            assert second_inside.wait(WATCHDOG_SECONDS)

    def second_reader() -> None:
        with FileLock(lock_path, shared=True):
            second_inside.set()
            assert first_inside.wait(WATCHDOG_SECONDS)

    threads = [
        threading.Thread(target=first_reader),
        threading.Thread(target=second_reader),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WATCHDOG_SECONDS)
        assert not thread.is_alive()
    assert first_inside.is_set()
    assert second_inside.is_set()
