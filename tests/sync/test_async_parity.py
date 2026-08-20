from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING

import pytest

from dr_store import BindStatus, ObjectStore, PutStatus, SqliteBackend
from dr_store.sync import open_sqlite

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def sqlite_path() -> Iterator[str]:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        yield handle.name


@pytest.mark.asyncio
async def test_async_put_bind_matches_blocking_resolve(
    sqlite_path: str,
) -> None:
    backend = await SqliteBackend.open(sqlite_path)
    try:
        async_store = ObjectStore(backend)
        reference, status = await async_store.put("demo.record", {"value": 1})
        assert status is PutStatus.STORED
        assert (
            await async_store.bind("shared-key", reference) is BindStatus.BOUND
        )
    finally:
        await backend.aclose()

    with open_sqlite(sqlite_path) as blocking_store:
        assert blocking_store.resolve("shared-key") == reference
        blocking_reference, blocking_status = blocking_store.put(
            "demo.record",
            {"value": 2},
        )
        assert blocking_status is PutStatus.STORED
        assert blocking_store.bind("blocking-key", blocking_reference) is (
            BindStatus.BOUND
        )

    backend = await SqliteBackend.open(sqlite_path)
    try:
        async_store = ObjectStore(backend)
        assert await async_store.resolve("blocking-key") == blocking_reference
    finally:
        await backend.aclose()
