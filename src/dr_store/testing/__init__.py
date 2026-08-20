"""Test helpers for dr-store consumers.

These helpers are for tests and examples only; they are not a supported
production surface.

``FakeClock`` drives expiry on the memory lease backend only. SQLite and
PostgreSQL read authority time from the database by design, so
``temp_sqlite_lease_authority`` cannot take a clock.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from dr_store.lease import LeaseAuthority
from dr_store.sync import BlockingObjectStore, open_sqlite

if TYPE_CHECKING:
    from collections.abc import Iterator


class FakeClock:
    def __init__(self, *, start: datetime | None = None) -> None:
        self._now = start or datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _unlink_sqlite_sidecars(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_suffix(path.suffix + "-wal").unlink(missing_ok=True)
    path.with_suffix(path.suffix + "-shm").unlink(missing_ok=True)


@contextmanager
def temp_sqlite_store() -> Iterator[BlockingObjectStore]:
    directory = Path(tempfile.mkdtemp(prefix="dr-store-test-"))
    path = directory / "store.sqlite3"
    try:
        with open_sqlite(str(path)) as store:
            yield store
    finally:
        _unlink_sqlite_sidecars(path)
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def temp_lease_authority() -> Iterator[tuple[LeaseAuthority, FakeClock]]:
    clock = FakeClock()
    authority = LeaseAuthority.memory(clock=clock.now)
    try:
        yield authority, clock
    finally:
        authority.close()


@contextmanager
def temp_sqlite_lease_authority() -> Iterator[LeaseAuthority]:
    directory = Path(tempfile.mkdtemp(prefix="dr-store-lease-test-"))
    path = directory / "lease.sqlite3"
    authority = LeaseAuthority.sqlite(path)
    try:
        yield authority
    finally:
        authority.close()
        _unlink_sqlite_sidecars(path)
        shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "FakeClock",
    "temp_lease_authority",
    "temp_sqlite_lease_authority",
    "temp_sqlite_store",
]
