from __future__ import annotations

import asyncio
import concurrent.futures
import tempfile
import threading
from typing import TYPE_CHECKING

import pytest

from dr_store import PutStatus
from dr_store.sync import (
    BlockingObjectStore,
    SyncSessionClosedError,
    close_all_persistent,
    close_persistent,
    open_sqlite,
    persistent_sqlite,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

    from dr_serialize import Jsonable

    from dr_store.content_addressing import ObjectReference

WATCHDOG_SECONDS = 15


@pytest.fixture
def sqlite_path() -> Iterator[str]:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name
    yield path
    close_persistent(path)
    close_all_persistent()


@pytest.fixture(autouse=True)
def _cleanup_persistent_registry() -> Iterator[None]:
    yield
    close_all_persistent()
    assert _loop_thread_count() == 0


class _GatedLock:
    def __init__(
        self,
        lock: threading.Lock,
        *,
        close_ready: threading.Event,
        register_gate: threading.Event,
        gate_active: threading.local,
    ) -> None:
        self._lock = lock
        self._close_ready = close_ready
        self._register_gate = register_gate
        self._gate_active = gate_active

    def __enter__(self) -> bool:
        if getattr(self._gate_active, "value", False):
            self._close_ready.set()
            assert self._register_gate.wait(timeout=WATCHDOG_SECONDS)
        return self._lock.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._lock.__exit__(exc_type, exc_value, traceback)


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


def _loop_thread_count() -> int:
    return sum(
        1
        for thread in threading.enumerate()
        if thread.name == "dr-store-sqlite-loop"
    )


def test_persistent_open_failure_cleans_up(sqlite_path: str) -> None:
    from unittest.mock import AsyncMock, patch

    from dr_store import SqliteBackend

    before = _loop_thread_count()

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

    assert _loop_thread_count() == before


def test_use_after_close_persistent_raises_typed_error(
    sqlite_path: str,
) -> None:
    store = persistent_sqlite(sqlite_path)
    reference, _ = store.put("demo.record", {"value": 1})
    close_persistent(sqlite_path)
    with pytest.raises(SyncSessionClosedError):
        store.get(reference)


def test_close_does_not_block_unrelated_paths() -> None:
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
        await asyncio.to_thread(release_close.wait, WATCHDOG_SECONDS)
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
            assert close_started.wait(timeout=WATCHDOG_SECONDS)
            opener.start()
            assert opened.wait(timeout=WATCHDOG_SECONDS)
            assert not opened_error
            release_close.set()
            closer.join(timeout=WATCHDOG_SECONDS)
            opener.join(timeout=WATCHDOG_SECONDS)
    finally:
        release_close.set()
        close_persistent(slow_path)
        close_persistent(fast_path)


def test_close_waits_for_in_flight_put(  # noqa: PLR0915
    sqlite_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dr_store import ObjectStore
    from dr_store.sync import blocking as blocking_module

    gate = threading.Event()
    put_started = threading.Event()
    drain_entered = threading.Event()
    put_done = threading.Event()
    close_done = threading.Event()
    original_put = ObjectStore.put
    original_drain = blocking_module._drain_inflight_futures

    async def slow_put(
        self: ObjectStore,
        schema: str,
        record: Jsonable,
    ) -> object:
        put_started.set()
        await asyncio.to_thread(gate.wait, WATCHDOG_SECONDS)
        return await original_put(self, schema, record)

    def observed_drain(
        snapshot: frozenset[concurrent.futures.Future[object]],
    ) -> None:
        drain_entered.set()
        original_drain(snapshot)

    store = persistent_sqlite(sqlite_path)
    reference_holder: list[ObjectReference] = []
    put_status_holder: list[PutStatus] = []
    put_error: list[BaseException] = []

    def run_put() -> None:
        try:
            reference, status = store.put("demo.record", {"value": 1})
            reference_holder.append(reference)
            put_status_holder.append(status)
        except Exception as exc:  # noqa: BLE001 - collect caller failures
            put_error.append(exc)
        finally:
            put_done.set()

    def run_close() -> None:
        close_persistent(sqlite_path)
        close_done.set()

    monkeypatch.setattr(
        blocking_module, "_drain_inflight_futures", observed_drain
    )
    monkeypatch.setattr(ObjectStore, "put", slow_put)
    try:
        worker = threading.Thread(target=run_put)
        worker.start()
        assert put_started.wait(timeout=WATCHDOG_SECONDS)
        closer = threading.Thread(target=run_close)
        closer.start()
        assert drain_entered.wait(timeout=WATCHDOG_SECONDS)
        assert not put_done.is_set()
        gate.set()
        worker.join(timeout=WATCHDOG_SECONDS)
        closer.join(timeout=WATCHDOG_SECONDS)
        assert not put_error
        assert put_status_holder == [PutStatus.STORED]
        assert close_done.is_set()
        with pytest.raises(SyncSessionClosedError):
            store.get(reference_holder[0])
    finally:
        gate.set()
        close_persistent(sqlite_path)


def test_straggler_register_after_close_raises(sqlite_path: str) -> None:
    from dr_store.sync import blocking as blocking_module

    register_gate = threading.Event()
    close_ready = threading.Event()
    gate_active = threading.local()

    put_error: list[BaseException] = []

    def run_put(session: blocking_module._StoreSession) -> None:
        gate_active.value = True
        try:
            assert session.store is not None
            session.store.put("demo.record", {"value": 1})
        except Exception as exc:  # noqa: BLE001 - collect caller failures
            put_error.append(exc)
        finally:
            gate_active.value = False

    def run_close() -> None:
        assert close_ready.wait(timeout=WATCHDOG_SECONDS)
        close_persistent(sqlite_path)
        register_gate.set()

    store = persistent_sqlite(sqlite_path)
    session = store._session
    assert session is not None
    real_lock = session._futures_lock
    gated_lock = _GatedLock(
        real_lock,
        close_ready=close_ready,
        register_gate=register_gate,
        gate_active=gate_active,
    )
    session._futures_lock = gated_lock  # ty: ignore[invalid-assignment]

    try:
        worker = threading.Thread(target=run_put, args=(session,))
        closer = threading.Thread(target=run_close)
        worker.start()
        closer.start()
        worker.join(timeout=WATCHDOG_SECONDS)
        closer.join(timeout=WATCHDOG_SECONDS)
        assert len(put_error) == 1
        assert isinstance(put_error[0], SyncSessionClosedError)
    finally:
        register_gate.set()
        session._futures_lock = real_lock
        close_persistent(sqlite_path)


def test_close_drains_failed_in_flight_put_and_completes_teardown(
    sqlite_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dr_store import ObjectStore
    from dr_store.core.errors import ObjectConflictError
    from dr_store.sync import blocking as blocking_module

    gate = threading.Event()
    put_started = threading.Event()
    drain_entered = threading.Event()
    close_done = threading.Event()
    original_drain = blocking_module._drain_inflight_futures

    async def failing_put(
        _self: ObjectStore,
        schema: str,
        _record: Jsonable,
    ) -> object:
        put_started.set()
        await asyncio.to_thread(gate.wait, WATCHDOG_SECONDS)
        raise ObjectConflictError(schema=schema, content_hash="0" * 64)

    def observed_drain(
        snapshot: frozenset[concurrent.futures.Future[object]],
    ) -> None:
        drain_entered.set()
        gate.set()
        original_drain(snapshot)

    store = persistent_sqlite(sqlite_path)
    put_error: list[BaseException] = []

    def run_put() -> None:
        try:
            store.put("demo.record", {"value": 1})
        except Exception as exc:  # noqa: BLE001 - collect caller failures
            put_error.append(exc)
        finally:
            gate.set()

    def run_close() -> None:
        close_persistent(sqlite_path)
        close_done.set()

    monkeypatch.setattr(
        blocking_module, "_drain_inflight_futures", observed_drain
    )
    monkeypatch.setattr(ObjectStore, "put", failing_put)
    try:
        worker = threading.Thread(target=run_put)
        worker.start()
        assert put_started.wait(timeout=WATCHDOG_SECONDS)
        closer = threading.Thread(target=run_close)
        closer.start()
        assert drain_entered.wait(timeout=WATCHDOG_SECONDS)
        worker.join(timeout=WATCHDOG_SECONDS)
        closer.join(timeout=WATCHDOG_SECONDS)
        assert len(put_error) == 1
        assert isinstance(put_error[0], ObjectConflictError)
        assert close_done.is_set()
        assert _loop_thread_count() == 0
    finally:
        gate.set()
        close_persistent(sqlite_path)


def test_close_all_persistent_drains_failed_op_on_first_session(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    from dr_store import ObjectStore
    from dr_store.core.errors import ObjectConflictError
    from dr_store.sync import blocking as blocking_module

    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as first_handle:
        first_path = first_handle.name
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as second_handle:
        second_path = second_handle.name

    gate = threading.Event()
    put_started = threading.Event()
    drain_entered = threading.Event()
    original_drain = blocking_module._drain_inflight_futures

    async def failing_put(
        _self: ObjectStore,
        schema: str,
        _record: Jsonable,
    ) -> object:
        put_started.set()
        await asyncio.to_thread(gate.wait, WATCHDOG_SECONDS)
        raise ObjectConflictError(schema=schema, content_hash="0" * 64)

    def observed_drain(
        snapshot: frozenset[concurrent.futures.Future[object]],
    ) -> None:
        drain_entered.set()
        gate.set()
        original_drain(snapshot)

    first_store = persistent_sqlite(first_path)
    second_store = persistent_sqlite(second_path)
    reference, _ = second_store.put("demo.record", {"value": 2})
    put_error: list[BaseException] = []

    def run_put() -> None:
        try:
            first_store.put("demo.record", {"value": 1})
        except Exception as exc:  # noqa: BLE001 - collect caller failures
            put_error.append(exc)
        finally:
            gate.set()

    monkeypatch.setattr(
        blocking_module, "_drain_inflight_futures", observed_drain
    )
    try:
        with patch.object(ObjectStore, "put", failing_put):
            worker = threading.Thread(target=run_put)
            worker.start()
            assert put_started.wait(timeout=WATCHDOG_SECONDS)
            closer = threading.Thread(target=close_all_persistent)
            closer.start()
            assert drain_entered.wait(timeout=WATCHDOG_SECONDS)
            closer.join(timeout=WATCHDOG_SECONDS)
            worker.join(timeout=WATCHDOG_SECONDS)
        # close_all_persistent iteration order is not contractually specified;
        # this test asserts end state only.
        assert len(put_error) == 1
        assert isinstance(put_error[0], ObjectConflictError)
        assert _loop_thread_count() == 0
        reopened = persistent_sqlite(second_path)
        assert reopened.get(reference) == {"value": 2}
    finally:
        gate.set()
        close_all_persistent()


def test_persistent_sqlite_finally_does_not_close_live_session(
    sqlite_path: str,
) -> None:
    from unittest.mock import patch

    from dr_store.sync import blocking as blocking_module

    store_a = persistent_sqlite(sqlite_path)
    reference, _ = store_a.put("demo.record", {"value": 1})
    original_open = blocking_module._StoreSession.open
    original_close = blocking_module._StoreSession.close
    close_calls: list[None] = []

    def open_clears_store_field(self: blocking_module._StoreSession) -> object:
        opened = original_open(self)
        self.store = None
        return opened

    def counting_close(self: blocking_module._StoreSession) -> None:
        close_calls.append(None)
        original_close(self)

    with (
        patch.object(
            blocking_module._StoreSession, "open", open_clears_store_field
        ),
        patch.object(blocking_module._StoreSession, "close", counting_close),
    ):
        store_b = persistent_sqlite(sqlite_path)

    assert store_b is store_a
    assert close_calls == []
    with blocking_module._sessions_lock:
        assert blocking_module._sessions.get(sqlite_path) is store_a._session
    assert store_a.get(reference) == {"value": 1}


def test_open_close_race_allowed_outcomes() -> None:
    from unittest.mock import patch

    from dr_store import SqliteBackend

    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name

    open_started = threading.Event()
    release_open = threading.Event()
    closer_done = threading.Event()
    original_open = SqliteBackend.open
    opener_store: list[BlockingObjectStore] = []
    opener_error: list[BaseException] = []

    async def slow_open(
        open_path: str,
        *,
        busy_timeout_ms: int = 30_000,
    ) -> SqliteBackend:
        open_started.set()
        await asyncio.to_thread(release_open.wait, WATCHDOG_SECONDS)
        return await original_open(open_path, busy_timeout_ms=busy_timeout_ms)

    def open_path() -> None:
        try:
            opener_store.append(persistent_sqlite(path))
        except Exception as exc:  # noqa: BLE001 - collect opener failures
            opener_error.append(exc)

    def close_path() -> None:
        assert open_started.wait(timeout=WATCHDOG_SECONDS)
        close_persistent(path)
        closer_done.set()

    try:
        with patch.object(SqliteBackend, "open", slow_open):
            opener = threading.Thread(target=open_path)
            closer = threading.Thread(target=close_path)
            opener.start()
            closer.start()
            release_open.set()
            opener.join(timeout=WATCHDOG_SECONDS)
            closer.join(timeout=WATCHDOG_SECONDS)
        assert closer_done.is_set()
        if opener_store:
            store = opener_store[0]
            with pytest.raises(SyncSessionClosedError):
                store.put("demo.record", {"value": 1})
        else:
            assert len(opener_error) == 1
            assert isinstance(opener_error[0], SyncSessionClosedError)
    finally:
        release_open.set()
        close_persistent(path)


def test_concurrent_open_failure_waiter_gets_open_error() -> None:
    from unittest.mock import AsyncMock, patch

    from dr_store import SqliteBackend

    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name

    before = _loop_thread_count()
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    async def failing_open(
        _path: str,
        *,
        _busy_timeout_ms: int = 30_000,
    ) -> SqliteBackend:
        raise OSError("cannot open sqlite backend")

    def open_path() -> None:
        start.wait(timeout=WATCHDOG_SECONDS)
        try:
            persistent_sqlite(path)
        except Exception as exc:  # noqa: BLE001 - collect opener failures
            errors.append(exc)

    try:
        with patch.object(
            SqliteBackend,
            "open",
            AsyncMock(side_effect=failing_open),
        ):
            threads = [threading.Thread(target=open_path) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait(timeout=WATCHDOG_SECONDS)
            for thread in threads:
                thread.join(timeout=WATCHDOG_SECONDS)
        assert len(errors) == 2
        assert all(
            isinstance(error, OSError)
            and "cannot open sqlite backend" in str(error)
            for error in errors
        )
        assert _loop_thread_count() == before
    finally:
        close_persistent(path)


def test_concurrent_open_failure_waiter_gets_kwonly_error() -> None:
    from unittest.mock import AsyncMock, patch

    from dr_store import SqliteBackend
    from dr_store.content_addressing import ObjectReference
    from dr_store.core.errors import ObjectNotFoundError

    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name

    before = _loop_thread_count()
    start = threading.Barrier(3)
    errors: list[BaseException] = []
    missing = ObjectReference(
        schema="demo.record",
        content_hash="0" * 64,
    )

    async def failing_open(
        _path: str,
        *,
        _busy_timeout_ms: int = 30_000,
    ) -> SqliteBackend:
        raise ObjectNotFoundError(reference=missing)

    def open_path() -> None:
        start.wait(timeout=WATCHDOG_SECONDS)
        try:
            persistent_sqlite(path)
        except Exception as exc:  # noqa: BLE001 - collect opener failures
            errors.append(exc)

    try:
        with patch.object(
            SqliteBackend,
            "open",
            AsyncMock(side_effect=failing_open),
        ):
            threads = [threading.Thread(target=open_path) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait(timeout=WATCHDOG_SECONDS)
            for thread in threads:
                thread.join(timeout=WATCHDOG_SECONDS)
        assert len(errors) == 2
        assert all(isinstance(error, ObjectNotFoundError) for error in errors)
        not_found = [
            error for error in errors if isinstance(error, ObjectNotFoundError)
        ]
        assert all(error.reference == missing for error in not_found)
        assert _loop_thread_count() == before
    finally:
        close_persistent(path)


def test_close_stops_thread_when_aclose_raises(sqlite_path: str) -> None:
    from unittest.mock import patch

    from dr_store import SqliteBackend

    before = _loop_thread_count()
    store = persistent_sqlite(sqlite_path)
    store.put("demo.record", {"value": 1})

    async def failing_aclose(_self: SqliteBackend) -> None:
        raise OSError("backend close failed")

    with patch.object(SqliteBackend, "aclose", failing_aclose):
        with pytest.raises(OSError, match="backend close failed"):
            close_persistent(sqlite_path)

    assert _loop_thread_count() == before


def test_close_all_persistent_closes_remaining_after_aclose_failure() -> None:
    from unittest.mock import patch

    from dr_store import SqliteBackend

    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as first_handle:
        first_path = first_handle.name
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as second_handle:
        second_path = second_handle.name

    before = _loop_thread_count()
    first_store = persistent_sqlite(first_path)
    second_store = persistent_sqlite(second_path)
    first_store.put("demo.record", {"value": 1})
    second_store.put("demo.record", {"value": 2})
    calls = {"count": 0}
    original_aclose = SqliteBackend.aclose

    async def flaky_aclose(_self: SqliteBackend) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("first backend close failed")
        await original_aclose(_self)

    try:
        with patch.object(SqliteBackend, "aclose", flaky_aclose):
            with pytest.raises(OSError, match="first backend close failed"):
                close_all_persistent()
        assert _loop_thread_count() == before
    finally:
        close_all_persistent()


def test_close_closes_event_loop(sqlite_path: str) -> None:
    store = persistent_sqlite(sqlite_path)
    session = store._session
    assert session is not None
    assert not session._loop.is_closed()
    close_persistent(sqlite_path)
    assert session._loop.is_closed()


def test_open_failure_reraise_preserves_operational_error() -> None:
    import sqlite3
    from unittest.mock import AsyncMock, patch

    from dr_store import SqliteBackend

    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name

    before = _loop_thread_count()
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    async def failing_open(
        _path: str,
        *,
        _busy_timeout_ms: int = 30_000,
    ) -> SqliteBackend:
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = 5
        exc.sqlite_errorname = "SQLITE_BUSY"
        raise exc

    def open_path() -> None:
        start.wait(timeout=WATCHDOG_SECONDS)
        try:
            persistent_sqlite(path)
        except Exception as exc:  # noqa: BLE001 - collect opener failures
            errors.append(exc)

    try:
        with patch.object(
            SqliteBackend,
            "open",
            AsyncMock(side_effect=failing_open),
        ):
            threads = [threading.Thread(target=open_path) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait(timeout=WATCHDOG_SECONDS)
            for thread in threads:
                thread.join(timeout=WATCHDOG_SECONDS)
        assert len(errors) == 2
        assert all(
            isinstance(error, sqlite3.OperationalError) for error in errors
        )
        assert all("database is locked" in str(error) for error in errors)
        assert _loop_thread_count() == before
    finally:
        close_persistent(path)
