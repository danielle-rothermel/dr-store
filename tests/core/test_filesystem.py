from __future__ import annotations

import errno
import os
import re
from typing import TYPE_CHECKING

import pytest

from dr_store.core import filesystem

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

FULL_FSYNC = 51


class _RecordingFcntl:
    # Linux CI cannot exercise the macOS F_FULLFSYNC branch natively.

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
    "unsupported_errno",
    [errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP],
    ids=["einval", "enotsup", "eopnotsupp"],
)
def test_unsupported_full_fsync_falls_back_to_fsync(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
    unsupported_errno: int,
) -> None:
    failure = OSError(unsupported_errno, "F_FULLFSYNC unsupported")
    fake = _RecordingFcntl(available=True, failure=failure)
    monkeypatch.setattr(filesystem, "fcntl", fake)
    synced = _record_fsync(monkeypatch)

    filesystem.flush_descriptor(descriptor)

    assert fake.commands == [(descriptor, FULL_FSYNC)]
    assert synced == [descriptor]


@pytest.mark.parametrize(
    "failure",
    [
        OSError(errno.EIO, "input/output error"),
        OSError(errno.EBADF, "bad descriptor"),
        OSError(errno.EINTR, "interrupted"),
        OSError("failure without errno"),
    ],
    ids=["eio", "ebadf", "eintr", "missing-errno"],
)
def test_full_fsync_failure_propagates_without_fsync_fallback(
    descriptor: int,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    fake = _RecordingFcntl(available=True, failure=failure)
    monkeypatch.setattr(filesystem, "fcntl", fake)
    synced = _record_fsync(monkeypatch)

    with pytest.raises(OSError, match=re.escape(str(failure))) as raised:
        filesystem.flush_descriptor(descriptor)

    assert raised.value is failure
    assert fake.commands == [(descriptor, FULL_FSYNC)]
    assert synced == []


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
