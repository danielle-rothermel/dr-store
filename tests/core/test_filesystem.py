"""The filesystem flush ladder: F_FULLFSYNC first, os.fsync as fallback.

macOS ``fsync`` only reaches the drive's write cache, so every commit point
tries ``F_FULLFSYNC`` and falls back to ``os.fsync`` -- where the fcntl
module or command is absent and after every ``F_FULLFSYNC`` ``OSError``. The
all-error fallback is accepted current behavior, not proof that a stronger
flush succeeded. CI runs on Linux, where only the fallback branch executes
naturally, so the ladder is driven here instead of by the ambient platform.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from dr_store.core import filesystem

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

FULL_FSYNC = 51


class _RecordingFcntl:
    """Stand-in for the fcntl module with a configurable F_FULLFSYNC."""

    def __init__(
        self,
        *,
        available: bool,
        failure: OSError | None = None,
    ) -> None:
        if available:
            self.F_FULLFSYNC = FULL_FSYNC
        self.failure = failure
        self.commands: list[tuple[int, int]] = []

    def fcntl(self, descriptor: int, command: int) -> int:
        self.commands.append((descriptor, command))
        if self.failure is not None:
            raise self.failure
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
    monkeypatch.setattr(filesystem.os, "fsync", synced.append)
    return synced


def test_full_fsync_is_used_when_available(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingFcntl(available=True)
    monkeypatch.setattr(filesystem, "fcntl", fake)
    synced = _record_fsync(monkeypatch)

    filesystem.flush_descriptor(descriptor)

    assert fake.commands == [(descriptor, FULL_FSYNC)]
    assert synced == []


@pytest.mark.parametrize(
    "failure",
    [
        OSError("F_FULLFSYNC unsupported on this filesystem"),
        OSError(5, "input/output error"),
    ],
    ids=["unsupported", "io-error"],
)
def test_every_full_fsync_oserror_falls_back_to_fsync(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    fake = _RecordingFcntl(available=True, failure=failure)
    monkeypatch.setattr(filesystem, "fcntl", fake)
    synced = _record_fsync(monkeypatch)

    filesystem.flush_descriptor(descriptor)

    assert fake.commands == [(descriptor, FULL_FSYNC)]
    assert synced == [descriptor]


def test_a_missing_full_fsync_command_falls_back_to_fsync(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingFcntl(available=False)
    monkeypatch.setattr(filesystem, "fcntl", fake)
    synced = _record_fsync(monkeypatch)

    filesystem.flush_descriptor(descriptor)

    assert fake.commands == []
    assert synced == [descriptor]


def test_a_missing_fcntl_module_falls_back_to_fsync(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(filesystem, "fcntl", None)
    synced = _record_fsync(monkeypatch)

    filesystem.flush_descriptor(descriptor)

    assert synced == [descriptor]


def test_flush_directory_flushes_the_directory_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[os.stat_result] = []
    monkeypatch.setattr(
        filesystem,
        "flush_descriptor",
        lambda descriptor: seen.append(os.fstat(descriptor)),
    )

    filesystem.flush_directory(tmp_path)

    assert len(seen) == 1
    assert seen[0].st_ino == tmp_path.stat().st_ino


def test_flush_directory_closes_its_descriptor_when_flush_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = 731
    closed: list[int] = []
    monkeypatch.setattr(filesystem.os, "open", lambda *_args: descriptor)
    monkeypatch.setattr(filesystem.os, "close", closed.append)

    class FlushError(OSError):
        pass

    def fail_flush(actual: int) -> None:
        assert actual == descriptor
        raise FlushError("flush failed")

    monkeypatch.setattr(filesystem, "flush_descriptor", fail_flush)

    with pytest.raises(FlushError):
        filesystem.flush_directory(tmp_path)

    assert closed == [descriptor]
