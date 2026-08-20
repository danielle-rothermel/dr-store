from __future__ import annotations

import concurrent.futures
import tempfile
from typing import TYPE_CHECKING

import pytest

from dr_store import PutStatus
from dr_store.sync import (
    SyncSessionClosedError,
    close_all_persistent,
    close_persistent,
    open_sqlite,
    persistent_sqlite,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dr_store.content_addressing import ObjectReference


@pytest.fixture
def sqlite_path() -> Iterator[str]:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name
    yield path
    close_persistent(path)


def test_put_get_round_trip(sqlite_path: str) -> None:
    with open_sqlite(sqlite_path) as store:
        reference, status = store.put("demo.record", {"value": 1})
        assert status is PutStatus.STORED
        assert store.get(reference) == {"value": 1}


def test_bind_resolve_evict(sqlite_path: str) -> None:
    from dr_store import BindStatus, EvictStatus

    with open_sqlite(sqlite_path) as store:
        reference, _ = store.put("demo.record", {"value": 1})
        assert store.bind("key", reference) is BindStatus.BOUND
        assert store.resolve("key") == reference
        evicted = store.evict_bindings(["key", "missing"])
        assert evicted == {
            "key": EvictStatus.EVICTED,
            "missing": EvictStatus.ABSENT,
        }


def test_concurrent_callers(sqlite_path: str) -> None:
    with open_sqlite(sqlite_path) as store:

        def write(index: int) -> tuple[ObjectReference, PutStatus]:
            return store.put("demo.record", {"index": index})

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(write, range(8)))
        assert all(status is PutStatus.STORED for _, status in results)
        assert len({reference.content_hash for reference, _ in results}) == 8


def test_session_lifecycle(sqlite_path: str) -> None:
    with open_sqlite(sqlite_path) as store:
        reference, _ = store.put("demo.record", {"value": 1})
    with open_sqlite(sqlite_path) as store:
        assert store.get(reference) == {"value": 1}


def test_persistent_lifecycle(sqlite_path: str) -> None:
    store = persistent_sqlite(sqlite_path)
    reference, _ = store.put("demo.record", {"value": 1})
    assert persistent_sqlite(sqlite_path) is store
    close_persistent(sqlite_path)
    close_persistent(sqlite_path)
    store = persistent_sqlite(sqlite_path)
    assert store.get(reference) == {"value": 1}
    close_all_persistent()


def test_error_propagation(sqlite_path: str) -> None:
    from dr_store.content_addressing import ObjectReference
    from dr_store.core.errors import ObjectNotFoundError

    with open_sqlite(sqlite_path) as store:
        missing = ObjectReference(
            schema="demo.record",
            content_hash="0" * 64,
        )
        with pytest.raises(ObjectNotFoundError):
            store.get(missing)


def test_concurrent_persistent_sqlite_opens_once(sqlite_path: str) -> None:
    import threading
    from unittest.mock import AsyncMock, patch

    from dr_store import SqliteBackend

    original_open = SqliteBackend.open
    call_count = 0
    lock = threading.Lock()

    async def counting_open(
        path: str,
        *,
        busy_timeout_ms: int = 30_000,
    ) -> SqliteBackend:
        nonlocal call_count
        with lock:
            call_count += 1
        return await original_open(path, busy_timeout_ms=busy_timeout_ms)

    with patch.object(
        SqliteBackend,
        "open",
        AsyncMock(side_effect=counting_open),
    ):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            stores = list(
                pool.map(lambda _: persistent_sqlite(sqlite_path), range(8))
            )
    assert call_count == 1
    assert len({id(store) for store in stores}) == 1
    close_persistent(sqlite_path)


def test_persistent_open_failure_cleans_up(sqlite_path: str) -> None:
    import threading
    from unittest.mock import AsyncMock, patch

    from dr_store import SqliteBackend

    before = {
        thread.name
        for thread in threading.enumerate()
        if thread.name == "dr-store-sqlite-loop"
    }

    async def failing_open(
        _path: str,
        *,
        _busy_timeout_ms: int = 30_000,
    ) -> SqliteBackend:
        raise OSError("cannot open sqlite backend")

    with patch.object(
        SqliteBackend,
        "open",
        AsyncMock(side_effect=failing_open),
    ):
        with pytest.raises(OSError, match="cannot open sqlite backend"):
            persistent_sqlite(sqlite_path)

    after = {
        thread.name
        for thread in threading.enumerate()
        if thread.name == "dr-store-sqlite-loop"
    }
    assert after == before


def test_use_after_close_persistent_raises_typed_error(
    sqlite_path: str,
) -> None:
    store = persistent_sqlite(sqlite_path)
    reference, _ = store.put("demo.record", {"value": 1})
    close_persistent(sqlite_path)
    with pytest.raises(SyncSessionClosedError):
        store.get(reference)


def test_close_does_not_block_unrelated_paths() -> None:
    import threading
    from unittest.mock import patch

    from dr_store import SqliteBackend

    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as slow_handle:
        slow_path = slow_handle.name
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as fast_handle:
        fast_path = fast_handle.name

    release_close = threading.Event()
    close_started = threading.Event()

    original_aclose = SqliteBackend.aclose

    async def slow_aclose(self: SqliteBackend) -> None:
        close_started.set()
        release_close.wait(timeout=5)
        await original_aclose(self)

    opened = threading.Event()
    opened_error: list[BaseException] = []

    def close_slow_path() -> None:
        persistent_sqlite(slow_path)
        close_persistent(slow_path)

    def open_fast_path() -> None:
        try:
            store = persistent_sqlite(fast_path)
            store.put("demo.record", {"value": 1})
        except Exception as exc:  # noqa: BLE001 - collect opener failures
            opened_error.append(exc)
        finally:
            opened.set()

    try:
        with patch.object(SqliteBackend, "aclose", slow_aclose):
            closer = threading.Thread(target=close_slow_path)
            opener = threading.Thread(target=open_fast_path)
            closer.start()
            assert close_started.wait(timeout=5)
            opener.start()
            assert opened.wait(timeout=5)
            assert not opened_error
            release_close.set()
            closer.join(timeout=5)
            opener.join(timeout=5)
    finally:
        release_close.set()
        close_persistent(slow_path)
        close_persistent(fast_path)
