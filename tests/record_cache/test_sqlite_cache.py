from __future__ import annotations

import queue
import sqlite3
import threading
from typing import TYPE_CHECKING

import pytest

from dr_store import (
    CacheHit,
    ObjectReference,
    ObjectStore,
    SqliteRecordCache,
    SqliteRecordCacheClosedError,
    SqliteRecordCacheCloseError,
)
from dr_store.record_cache import sqlite as sqlite_module

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from dr_serialize import Jsonable

KEY = "example.memo.v1:key"
SCHEMA = "example.record"
RECORD: Jsonable = {"payload": {"a": 1, "b": [2, 3]}}
WATCHDOG_SECONDS = 15


class _ObservedCondition(threading.Condition):
    def __init__(self, waiting: threading.Event) -> None:
        super().__init__()
        self._waiting = waiting

    def wait(self, timeout: float | None = None) -> bool:
        self._waiting.set()
        return super().wait(timeout)


class _InterruptingDrainCondition(threading.Condition):
    def __init__(
        self,
        interruption: RuntimeError,
        elected_waiting: threading.Event,
        concurrent_waiting: threading.Event,
    ) -> None:
        super().__init__()
        self._interruption = interruption
        self._elected_waiting = elected_waiting
        self._concurrent_waiting = concurrent_waiting
        self._interrupted = False

    def wait(self, timeout: float | None = None) -> bool:
        if (
            threading.current_thread().name == "interrupted-closer"
            and not self._interrupted
        ):
            self._interrupted = True
            self._elected_waiting.set()
            super().wait(timeout)
            raise self._interruption
        self._concurrent_waiting.set()
        return super().wait(timeout)


def _capture(
    operation: Callable[[], object],
    results: queue.Queue[object],
) -> None:
    try:
        results.put(operation())
    except Exception as error:  # noqa: BLE001 - propagate worker failures.
        results.put(error)


def _join(thread: threading.Thread) -> None:
    thread.join(WATCHDOG_SECONDS)
    assert not thread.is_alive(), f"{thread.name} exceeded its watchdog"


def _await_closing(cache: SqliteRecordCache) -> None:
    with cache._lifecycle:
        reached = cache._lifecycle.wait_for(
            lambda: cache._state is sqlite_module._Lifecycle.CLOSING,
            WATCHDOG_SECONDS,
        )
    assert reached, "cache did not begin closing"


def _use_cache_then_raise(
    cache: SqliteRecordCache,
    failure: RuntimeError,
) -> None:
    with cache:
        cache.put(KEY, SCHEMA, RECORD)
        raise failure


@pytest.mark.parametrize("path", ["", ":memory:"])
def test_construction_rejects_transient_database_paths(path: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        SqliteRecordCache(path)


def test_records_persist_across_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "cache.db"
    first = SqliteRecordCache(path)
    reference = first.put(KEY, SCHEMA, RECORD)
    first.close()

    with SqliteRecordCache(path) as reopened:
        assert reopened.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)
        assert reopened.put(KEY, SCHEMA, RECORD) == reference


def test_context_manager_closes_after_normal_block(tmp_path: Path) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")

    with cache as entered:
        assert entered is cache
        cache.put(KEY, SCHEMA, RECORD)

    with pytest.raises(SqliteRecordCacheClosedError):
        cache.get(KEY, schema=SCHEMA)


def test_context_manager_closes_without_swallowing_body_exception(
    tmp_path: Path,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    body_failure = RuntimeError()

    with pytest.raises(RuntimeError) as caught:
        _use_cache_then_raise(cache, body_failure)

    assert caught.value is body_failure

    with pytest.raises(SqliteRecordCacheClosedError):
        cache.get(KEY, schema=SCHEMA)


def test_context_cleanup_failure_replaces_body_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    body_failure = RuntimeError()
    cleanup_failure = RuntimeError()

    def fail_cleanup() -> None:
        raise cleanup_failure

    monkeypatch.setattr(
        cache._sqlite_backend,
        "_close_connections",
        fail_cleanup,
    )

    with pytest.raises(SqliteRecordCacheCloseError) as caught:
        _use_cache_then_raise(cache, body_failure)

    assert caught.value.__cause__ is cleanup_failure
    assert cleanup_failure.__context__ is body_failure
    with pytest.raises(SqliteRecordCacheCloseError) as repeated:
        cache.close()
    assert repeated.value.__cause__ is cleanup_failure
    with pytest.raises(SqliteRecordCacheClosedError):
        cache.get(KEY, schema=SCHEMA)


def test_closed_operations_fail_before_input_validation(
    tmp_path: Path,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    cache.close()

    with pytest.raises(SqliteRecordCacheClosedError):
        cache.get(
            None,  # ty: ignore[invalid-argument-type]
            schema=None,  # ty: ignore[invalid-argument-type]
        )
    with pytest.raises(SqliteRecordCacheClosedError):
        cache.put(
            None,  # ty: ignore[invalid-argument-type]
            None,  # ty: ignore[invalid-argument-type]
            object(),  # ty: ignore[invalid-argument-type]
        )


def test_repeated_and_concurrent_close_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    cache.get(KEY, schema=SCHEMA)
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    closer_waiting = threading.Event()
    cache._lifecycle = _ObservedCondition(closer_waiting)
    close_connections = cache._sqlite_backend._close_connections

    def gated_cleanup() -> None:
        cleanup_started.set()
        assert release_cleanup.wait(WATCHDOG_SECONDS)
        close_connections()

    monkeypatch.setattr(
        cache._sqlite_backend,
        "_close_connections",
        gated_cleanup,
    )
    results: queue.Queue[object] = queue.Queue()
    elected = threading.Thread(
        target=_capture,
        args=(cache.close, results),
        name="elected-closer",
    )
    concurrent = threading.Thread(
        target=_capture,
        args=(cache.close, results),
        name="concurrent-closer",
    )

    elected.start()
    assert cleanup_started.wait(WATCHDOG_SECONDS)
    concurrent.start()
    assert closer_waiting.wait(WATCHDOG_SECONDS)
    release_cleanup.set()
    _join(elected)
    _join(concurrent)

    assert [results.get_nowait(), results.get_nowait()] == [None, None]
    cache.close()


def test_close_waits_for_complete_get_and_rejects_new_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    cache.put(KEY, SCHEMA, RECORD)
    get_started = threading.Event()
    release_get = threading.Event()
    original_get = ObjectStore.get

    def gated_get(
        store: ObjectStore,
        reference: ObjectReference,
    ) -> Jsonable:
        get_started.set()
        assert release_get.wait(WATCHDOG_SECONDS)
        return original_get(store, reference)

    monkeypatch.setattr(ObjectStore, "get", gated_get)
    results: queue.Queue[object] = queue.Queue()
    reader = threading.Thread(
        target=_capture,
        args=(lambda: cache.get(KEY, schema=SCHEMA), results),
        name="active-get",
    )
    closer = threading.Thread(
        target=_capture,
        args=(cache.close, results),
        name="waiting-closer",
    )

    reader.start()
    assert get_started.wait(WATCHDOG_SECONDS)
    closer.start()
    _await_closing(cache)
    with pytest.raises(SqliteRecordCacheClosedError):
        cache.get(
            None,  # ty: ignore[invalid-argument-type]
            schema=None,  # ty: ignore[invalid-argument-type]
        )

    release_get.set()
    _join(reader)
    _join(closer)

    observed = [results.get_nowait(), results.get_nowait()]
    assert CacheHit(record=RECORD) in observed
    assert None in observed


def test_close_waits_for_complete_put_and_reopen_observes_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cache.db"
    cache = SqliteRecordCache(path)
    bind_started = threading.Event()
    release_bind = threading.Event()
    original_bind = ObjectStore.bind

    def gated_bind(
        store: ObjectStore,
        key: str,
        reference: ObjectReference,
    ) -> object:
        bind_started.set()
        assert release_bind.wait(WATCHDOG_SECONDS)
        return original_bind(store, key, reference)

    monkeypatch.setattr(ObjectStore, "bind", gated_bind)
    results: queue.Queue[object] = queue.Queue()
    writer = threading.Thread(
        target=_capture,
        args=(lambda: cache.put(KEY, SCHEMA, RECORD), results),
        name="active-put",
    )
    closer = threading.Thread(
        target=_capture,
        args=(cache.close, results),
        name="waiting-closer",
    )

    writer.start()
    assert bind_started.wait(WATCHDOG_SECONDS)
    closer.start()
    _await_closing(cache)
    with pytest.raises(SqliteRecordCacheClosedError):
        cache.put(
            None,  # ty: ignore[invalid-argument-type]
            None,  # ty: ignore[invalid-argument-type]
            object(),  # ty: ignore[invalid-argument-type]
        )

    release_bind.set()
    _join(writer)
    _join(closer)

    observed = [results.get_nowait(), results.get_nowait()]
    assert any(isinstance(result, ObjectReference) for result in observed)
    assert None in observed
    with SqliteRecordCache(path) as reopened:
        assert reopened.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)


def test_same_thread_reentrant_close_fails_without_closing_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    cache.put(KEY, SCHEMA, RECORD)
    original_resolve = ObjectStore.resolve
    close_errors: list[SqliteRecordCacheCloseError] = []

    def resolve_and_try_close(
        store: ObjectStore,
        key: str,
    ) -> ObjectReference | None:
        with pytest.raises(SqliteRecordCacheCloseError) as raised:
            cache.close()
        close_errors.append(raised.value)
        return original_resolve(store, key)

    monkeypatch.setattr(ObjectStore, "resolve", resolve_and_try_close)

    assert cache.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)
    assert len(close_errors) == 1
    cache.close()


def test_drain_interruption_wakes_a_concurrent_close_caller(
    tmp_path: Path,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    operation_started = threading.Event()
    release_operation = threading.Event()
    elected_waiting = threading.Event()
    concurrent_waiting = threading.Event()
    interruption = RuntimeError()
    results: queue.Queue[object] = queue.Queue()

    def active_operation() -> None:
        with cache._admit_operation():
            operation_started.set()
            assert release_operation.wait(WATCHDOG_SECONDS)

    worker = threading.Thread(
        target=_capture,
        args=(active_operation, results),
        name="active-operation",
    )
    worker.start()
    assert operation_started.wait(WATCHDOG_SECONDS)
    cache._lifecycle = _InterruptingDrainCondition(
        interruption,
        elected_waiting,
        concurrent_waiting,
    )
    interrupted = threading.Thread(
        target=_capture,
        args=(cache.close, results),
        name="interrupted-closer",
    )
    concurrent = threading.Thread(
        target=_capture,
        args=(cache.close, results),
        name="concurrent-closer",
    )

    interrupted.start()
    assert elected_waiting.wait(WATCHDOG_SECONDS)
    concurrent.start()
    assert concurrent_waiting.wait(WATCHDOG_SECONDS)

    release_operation.set()
    _join(worker)
    _join(interrupted)
    _join(concurrent)

    observed = [results.get_nowait() for _ in range(3)]
    assert any(result is interruption for result in observed)
    assert observed.count(None) == 2
    with pytest.raises(SqliteRecordCacheClosedError):
        cache.get(KEY, schema=SCHEMA)
    cache.close()


def test_process_level_cleanup_failure_terminalizes_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    cache.get(KEY, schema=SCHEMA)
    interruption = KeyboardInterrupt("injected cleanup interruption")

    def interrupt_cleanup() -> None:
        raise interruption

    monkeypatch.setattr(
        cache._sqlite_backend,
        "_close_connections",
        interrupt_cleanup,
    )

    with pytest.raises(KeyboardInterrupt) as first:
        cache.close()
    assert first.value is interruption
    with pytest.raises(SqliteRecordCacheCloseError) as repeated:
        cache.close()
    assert repeated.value.__cause__ is interruption
    with pytest.raises(SqliteRecordCacheClosedError):
        cache.get(KEY, schema=SCHEMA)


def test_cleanup_failure_is_terminal_for_all_close_callers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    cache.get(KEY, schema=SCHEMA)
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    closer_waiting = threading.Event()
    cache._lifecycle = _ObservedCondition(closer_waiting)
    close_connections = cache._sqlite_backend._close_connections
    cleanup_failure = RuntimeError("injected cleanup failure")

    def failing_cleanup() -> None:
        cleanup_started.set()
        assert release_cleanup.wait(WATCHDOG_SECONDS)
        close_connections()
        raise cleanup_failure

    monkeypatch.setattr(
        cache._sqlite_backend,
        "_close_connections",
        failing_cleanup,
    )
    results: queue.Queue[object] = queue.Queue()
    elected = threading.Thread(
        target=_capture,
        args=(cache.close, results),
        name="failing-closer",
    )
    concurrent = threading.Thread(
        target=_capture,
        args=(cache.close, results),
        name="concurrent-closer",
    )

    elected.start()
    assert cleanup_started.wait(WATCHDOG_SECONDS)
    with pytest.raises(SqliteRecordCacheClosedError):
        cache.get(KEY, schema=SCHEMA)
    concurrent.start()
    assert closer_waiting.wait(WATCHDOG_SECONDS)
    release_cleanup.set()
    _join(elected)
    _join(concurrent)

    close_results = [results.get_nowait(), results.get_nowait()]
    for result in close_results:
        assert isinstance(result, SqliteRecordCacheCloseError)
        assert result.__cause__ is cleanup_failure
    with pytest.raises(SqliteRecordCacheCloseError) as repeated:
        cache.close()
    assert repeated.value.__cause__ is cleanup_failure
    with pytest.raises(SqliteRecordCacheClosedError):
        cache.put(KEY, SCHEMA, RECORD)


def test_close_centrally_closes_worker_thread_connections(
    tmp_path: Path,
) -> None:
    cache = SqliteRecordCache(tmp_path / "cache.db")
    ready = threading.Barrier(3)
    release_workers = threading.Event()
    results: queue.Queue[object] = queue.Queue()

    def read_from_worker() -> None:
        cache.get(KEY, schema=SCHEMA)
        ready.wait(WATCHDOG_SECONDS)
        assert release_workers.wait(WATCHDOG_SECONDS)

    workers = [
        threading.Thread(
            target=_capture,
            args=(read_from_worker, results),
            name=f"cache-worker-{index}",
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()

    try:
        ready.wait(WATCHDOG_SECONDS)
        with cache._sqlite_backend._connections_lock:
            connections = tuple(cache._sqlite_backend._connections)
    finally:
        release_workers.set()
        for worker in workers:
            _join(worker)

    assert [results.get_nowait(), results.get_nowait()] == [None, None]
    assert len(connections) == 2
    cache.close()
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_same_path_instances_have_isolated_lifecycles(tmp_path: Path) -> None:
    path = tmp_path / "cache.db"
    first = SqliteRecordCache(path)
    second = SqliteRecordCache(path)
    first.put(KEY, SCHEMA, RECORD)
    assert second.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)

    first.close()

    with pytest.raises(SqliteRecordCacheClosedError):
        first.get(KEY, schema=SCHEMA)
    assert second.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)
    second.close()

    with SqliteRecordCache(path) as reopened:
        assert reopened.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)
