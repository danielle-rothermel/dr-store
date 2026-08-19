"""Test helpers for dr-store consumers.

``FakeClock`` drives expiry on the memory lease backend only. SQLite and
PostgreSQL read authority time from the database by design, so
``temp_sqlite_lease_authority`` cannot take a clock.
"""

from __future__ import annotations

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


@contextmanager
def temp_sqlite_store() -> Iterator[BlockingObjectStore]:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name
    try:
        with open_sqlite(path) as store:
            yield store
    finally:
        Path(path).unlink(missing_ok=True)


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
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name
    authority = LeaseAuthority.sqlite(path)
    try:
        yield authority
    finally:
        authority.close()
        Path(path).unlink(missing_ok=True)


__all__ = [
    "FakeClock",
    "temp_lease_authority",
    "temp_sqlite_lease_authority",
    "temp_sqlite_store",
]
