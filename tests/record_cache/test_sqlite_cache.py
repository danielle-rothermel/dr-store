from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from dr_store import (
    CacheEntry,
    CacheHit,
    SqliteRecordCache,
    SqliteRecordCacheClosedError,
    SqliteRecordCacheCloseError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from dr_serialize import Jsonable

    from dr_store import BoundObjectRow

KEY = "example.memo.v1:key"
SCHEMA = "example.record"
RECORD: Jsonable = {"payload": {"a": 1, "b": [2, 3]}}
WATCHDOG_SECONDS = 15


def test_direct_construction_is_private(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match=r"await SqliteRecordCache\.open"):
        SqliteRecordCache(tmp_path / "cache.db")


@pytest.mark.parametrize("path", ["", ":memory:"])
async def test_open_rejects_transient_paths(path: str) -> None:
    with pytest.raises(ValueError, match="persistent filesystem path"):
        await SqliteRecordCache.open(path)


async def test_open_forwards_busy_timeout_ms_to_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dr_store.storage_backends import sqlite as sqlite_backend

    observed: list[int] = []
    original_open = sqlite_backend.SqliteBackend.open

    async def capturing_open(
        path: str | Path,
        *,
        busy_timeout_ms: int = 30_000,
    ) -> sqlite_backend.SqliteBackend:
        observed.append(busy_timeout_ms)
        return await original_open(path, busy_timeout_ms=busy_timeout_ms)

    monkeypatch.setattr(
        sqlite_backend.SqliteBackend,
        "open",
        capturing_open,
    )
    cache = await SqliteRecordCache.open(
        tmp_path / "cache.db",
        busy_timeout_ms=7_500,
    )
    await cache.aclose()
    assert observed == [7_500]


async def test_records_persist_across_close_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache.db"
    first = await SqliteRecordCache.open(path)
    entries = {
        KEY: CacheEntry(SCHEMA, RECORD),
        "null": CacheEntry(SCHEMA, None),
    }
    references = await first.put_many(entries)
    await first.aclose()
    await first.aclose()

    async with await SqliteRecordCache.open(path) as reopened:
        assert await reopened.get_many(entries, schema=SCHEMA) == {
            KEY: CacheHit(RECORD),
            "null": CacheHit(None),
        }
        assert await reopened.put_many(entries) == references


async def test_async_context_closes_without_suppressing_body_failure(
    tmp_path: Path,
) -> None:
    cache = await SqliteRecordCache.open(tmp_path / "cache.db")
    body_failure = RuntimeError("body failed")

    async def use_then_raise() -> None:
        async with cache as entered:
            assert entered is cache
            await cache.put(KEY, SCHEMA, RECORD)
            raise body_failure

    with pytest.raises(RuntimeError) as caught:
        await use_then_raise()
    assert caught.value is body_failure
    with pytest.raises(SqliteRecordCacheClosedError):
        await cache.get(KEY, schema=SCHEMA)


async def test_closed_error_precedes_input_validation(tmp_path: Path) -> None:
    cache = await SqliteRecordCache.open(tmp_path / "cache.db")
    await cache.aclose()
    with pytest.raises(SqliteRecordCacheClosedError):
        await cache.get(
            None,  # ty: ignore[invalid-argument-type]
            schema=None,  # ty: ignore[invalid-argument-type]
        )
    with pytest.raises(SqliteRecordCacheClosedError):
        await cache.put_many(None)  # ty: ignore[invalid-argument-type]


async def test_close_waits_for_whole_cache_operation_and_rejects_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = await SqliteRecordCache.open(tmp_path / "cache.db")
    await cache.put(KEY, SCHEMA, RECORD)
    started = asyncio.Event()
    release = asyncio.Event()
    close_started = asyncio.Event()
    original = cache._store._get_bound_objects
    close_resources = cache._close_resources

    async def gated_get(
        keys: tuple[str, ...],
    ) -> Mapping[str, BoundObjectRow]:
        started.set()
        await release.wait()
        return await original(keys)

    async def observed_close() -> None:
        close_started.set()
        await close_resources()

    monkeypatch.setattr(cache._store, "_get_bound_objects", gated_get)
    monkeypatch.setattr(cache, "_close_resources", observed_close)
    operation = asyncio.create_task(cache.get_many([KEY], schema=SCHEMA))
    await asyncio.wait_for(started.wait(), WATCHDOG_SECONDS)
    closing = asyncio.create_task(cache.aclose())
    await asyncio.wait_for(close_started.wait(), WATCHDOG_SECONDS)
    with pytest.raises(SqliteRecordCacheClosedError):
        await cache.get_many(
            None,  # ty: ignore[invalid-argument-type]
            schema=None,  # ty: ignore[invalid-argument-type]
        )
    release.set()
    assert await operation == {KEY: CacheHit(RECORD)}
    await closing


async def test_cancelled_close_waiter_does_not_abandon_shared_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = await SqliteRecordCache.open(tmp_path / "cache.db")
    started = asyncio.Event()
    release = asyncio.Event()
    close_started = asyncio.Event()
    original = cache._store._get_bound_objects
    close_resources = cache._close_resources

    async def gated_get(
        keys: tuple[str, ...],
    ) -> Mapping[str, BoundObjectRow]:
        started.set()
        await release.wait()
        return await original(keys)

    async def observed_close() -> None:
        close_started.set()
        await close_resources()

    monkeypatch.setattr(cache._store, "_get_bound_objects", gated_get)
    monkeypatch.setattr(cache, "_close_resources", observed_close)
    operation = asyncio.create_task(cache.get(KEY, schema=SCHEMA))
    await asyncio.wait_for(started.wait(), WATCHDOG_SECONDS)
    cancelled_waiter = asyncio.create_task(cache.aclose())
    await asyncio.wait_for(close_started.wait(), WATCHDOG_SECONDS)
    cancelled_waiter.cancel()
    release.set()
    assert await operation is None
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    await cache.aclose()
    assert cache._state.name == "CLOSED"


async def test_concurrent_close_is_idempotent(tmp_path: Path) -> None:
    cache = await SqliteRecordCache.open(tmp_path / "cache.db")
    await asyncio.gather(cache.aclose(), cache.aclose(), cache.aclose())
    await cache.aclose()


async def test_cleanup_failure_is_shared_and_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = await SqliteRecordCache.open(tmp_path / "cache.db")
    cleanup_failure = RuntimeError("injected cleanup failure")

    async def fail_cleanup() -> None:
        raise cleanup_failure

    monkeypatch.setattr(cache._sqlite_backend, "aclose", fail_cleanup)
    results = await asyncio.gather(
        cache.aclose(), cache.aclose(), return_exceptions=True
    )
    assert len(results) == 2
    for result in results:
        assert isinstance(result, SqliteRecordCacheCloseError)
        assert result.__cause__ is cleanup_failure
    with pytest.raises(SqliteRecordCacheCloseError) as repeated:
        await cache.aclose()
    assert repeated.value.__cause__ is cleanup_failure
    with pytest.raises(SqliteRecordCacheClosedError):
        await cache.put(KEY, SCHEMA, RECORD)


async def test_close_from_active_operation_fails_without_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = await SqliteRecordCache.open(tmp_path / "cache.db")
    original = cache._store._get_bound_objects
    observed: list[SqliteRecordCacheCloseError] = []

    async def close_during_get(
        keys: tuple[str, ...],
    ) -> Mapping[str, BoundObjectRow]:
        with pytest.raises(SqliteRecordCacheCloseError) as caught:
            await cache.aclose()
        observed.append(caught.value)
        return await original(keys)

    monkeypatch.setattr(cache._store, "_get_bound_objects", close_during_get)
    assert await cache.get(KEY, schema=SCHEMA) is None
    assert len(observed) == 1
    await cache.aclose()


async def test_same_path_instances_have_independent_lifecycles(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache.db"
    first = await SqliteRecordCache.open(path)
    second = await SqliteRecordCache.open(path)
    await first.put(KEY, SCHEMA, RECORD)
    assert await second.get(KEY, schema=SCHEMA) == CacheHit(RECORD)
    await first.aclose()
    assert await second.get(KEY, schema=SCHEMA) == CacheHit(RECORD)
    await second.aclose()


async def test_cross_loop_use_fails_visibly(tmp_path: Path) -> None:
    cache = await SqliteRecordCache.open(tmp_path / "cache.db")

    def other_loop() -> BaseException:
        try:
            asyncio.run(cache.get(KEY, schema=SCHEMA))
        except BaseException as error:  # noqa: BLE001 - inspect boundary.
            return error
        raise AssertionError("cross-loop operation unexpectedly succeeded")

    error = await asyncio.to_thread(other_loop)
    assert isinstance(error, RuntimeError)
    assert "event loop" in str(error)
    await cache.aclose()
