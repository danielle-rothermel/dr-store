"""The durability flush ladder: F_FULLFSYNC first, os.fsync as fallback.

macOS ``fsync`` only reaches the drive's write cache, so every commit point
tries ``F_FULLFSYNC`` and falls back to ``os.fsync`` -- where the fcntl
command is absent and where the filesystem rejects it. CI runs on Linux,
where only the fallback branch executes naturally, so the ladder is driven
here with monkeypatched descriptors instead of the ambient platform.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from dr_store import docdir

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

FULL_FSYNC = 51  # The macOS F_FULLFSYNC command value.


class _RecordingFcntl:
    """Stand-in for the fcntl module with a configurable F_FULLFSYNC."""

    def __init__(self, *, available: bool, refuses: bool = False) -> None:
        if available:
            self.F_FULLFSYNC = FULL_FSYNC
        self.refuses = refuses
        self.commands: list[tuple[int, int]] = []

    def fcntl(self, descriptor: int, command: int) -> int:
        self.commands.append((descriptor, command))
        if self.refuses:
            raise OSError("F_FULLFSYNC unsupported on this filesystem")
        return 0


@pytest.fixture
def descriptor(tmp_path: Path) -> Iterator[int]:
    path = tmp_path / "flushed.bin"
    path.write_bytes(b"payload")
    handle = os.open(path, os.O_RDONLY)
    yield handle
    os.close(handle)


def _record_fsync(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    synced: list[int] = []
    monkeypatch.setattr(docdir.os, "fsync", synced.append)
    return synced


def test_full_fsync_is_used_when_available(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingFcntl(available=True)
    monkeypatch.setattr(docdir, "fcntl", fake)
    synced = _record_fsync(monkeypatch)

    docdir._flush_descriptor(descriptor)

    assert fake.commands == [(descriptor, FULL_FSYNC)]
    assert synced == []


def test_a_refused_full_fsync_falls_back_to_fsync(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingFcntl(available=True, refuses=True)
    monkeypatch.setattr(docdir, "fcntl", fake)
    synced = _record_fsync(monkeypatch)

    docdir._flush_descriptor(descriptor)

    assert fake.commands == [(descriptor, FULL_FSYNC)]
    assert synced == [descriptor]


def test_a_missing_full_fsync_command_falls_back_to_fsync(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingFcntl(available=False)
    monkeypatch.setattr(docdir, "fcntl", fake)
    synced = _record_fsync(monkeypatch)

    docdir._flush_descriptor(descriptor)

    assert fake.commands == []
    assert synced == [descriptor]


def test_a_missing_fcntl_module_falls_back_to_fsync(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fcntl does not exist off POSIX; the package still imports and the
    # ladder still reaches the storage medium through os.fsync.
    monkeypatch.setattr(docdir, "fcntl", None)
    synced = _record_fsync(monkeypatch)

    docdir._flush_descriptor(descriptor)

    assert synced == [descriptor]


def test_flush_directory_flushes_the_directory_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[os.stat_result] = []
    monkeypatch.setattr(
        docdir,
        "_flush_descriptor",
        lambda descriptor: seen.append(os.fstat(descriptor)),
    )

    docdir._flush_directory(tmp_path)

    assert len(seen) == 1
    assert seen[0].st_ino == tmp_path.stat().st_ino
