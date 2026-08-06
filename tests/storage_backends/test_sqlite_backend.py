from __future__ import annotations

import multiprocessing
import queue
import sqlite3
import threading
from typing import TYPE_CHECKING, Literal

import pytest

from dr_store import BindOutcome, PutOutcome, SqliteBackend

if TYPE_CHECKING:
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from multiprocessing.synchronize import Event
    from pathlib import Path

SCHEMA = "example.record"
CONTENT_HASH = "a" * 64
CANONICAL = '{"value":"stored"}'
KEY = "durable-key"
PROCESS_CONTENDERS = 6
WATCHDOG_SECONDS = 15

Operation = Literal["put", "bind"]
ProcessResult = tuple[str, int, bool, str, str]
Contender = tuple[str, Operation, int, str]


class _FakeConnection:
    def __init__(
        self,
        *,
        fail_on_execute: int | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.fail_on_execute = fail_on_execute
        self.close_error = close_error
        self.execute_calls = 0
        self.close_calls = 0

    def execute(self, _statement: str) -> None:
        self.execute_calls += 1
        if self.execute_calls == self.fail_on_execute:
            raise RuntimeError("injected PRAGMA failure")

    def executescript(self, _script: str) -> None:
        pass

    def commit(self) -> None:
        pass

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def test_initialization_explicitly_closes_its_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()

    def connect(_path: str, **_settings: object) -> _FakeConnection:
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)

    SqliteBackend(tmp_path / "store.db")

    assert connection.close_calls == 1


def test_partial_connection_setup_failure_closes_before_reraising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(fail_on_execute=2)

    def connect(_path: str, **_settings: object) -> _FakeConnection:
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)

    with pytest.raises(RuntimeError, match="injected PRAGMA failure"):
        SqliteBackend(tmp_path / "store.db")

    assert connection.close_calls == 1


def _collect_thread_connections(
    backend: SqliteBackend,
    count: int,
) -> list[sqlite3.Connection]:
    ready = threading.Barrier(count + 1)
    release = threading.Event()
    connections: queue.Queue[sqlite3.Connection] = queue.Queue()

    def connect() -> None:
        first = backend._conn
        ready.wait(WATCHDOG_SECONDS)
        if not release.wait(WATCHDOG_SECONDS):
            return
        assert backend._conn is first
        connections.put(first)

    threads = [threading.Thread(target=connect) for _ in range(count)]
    for thread in threads:
        thread.start()

    try:
        ready.wait(WATCHDOG_SECONDS)
    finally:
        release.set()
        for thread in threads:
            thread.join(WATCHDOG_SECONDS)

    assert all(not thread.is_alive() for thread in threads)
    return [connections.get_nowait() for _ in range(count)]


def test_operational_connections_are_retained_separately_per_thread(
    tmp_path: Path,
) -> None:
    backend = SqliteBackend(tmp_path / "store.db")

    connections = _collect_thread_connections(backend, 2)

    assert connections[0] is not connections[1]


def test_close_connections_centrally_closes_all_worker_connections(
    tmp_path: Path,
) -> None:
    backend = SqliteBackend(tmp_path / "store.db")
    connections = _collect_thread_connections(backend, 2)

    backend._close_connections()

    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
    backend._close_connections()


def test_close_connections_attempts_every_close_before_reporting_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialization = _FakeConnection()
    failed = _FakeConnection(
        close_error=RuntimeError("injected close failure")
    )
    succeeded = _FakeConnection()
    available = queue.Queue[_FakeConnection]()
    for connection in (initialization, failed, succeeded):
        available.put(connection)

    def connect(_path: str, **_settings: object) -> _FakeConnection:
        return available.get_nowait()

    monkeypatch.setattr(sqlite3, "connect", connect)
    backend = SqliteBackend(tmp_path / "store.db")
    _collect_thread_connections(backend, 2)

    with pytest.raises(
        ExceptionGroup,
        match="failed to close SQLite operational connections",
    ) as raised:
        backend._close_connections()

    assert [str(error) for error in raised.value.exceptions] == [
        "injected close failure"
    ]
    assert failed.close_calls == 1
    assert succeeded.close_calls == 1


def test_rows_and_replay_outcomes_persist_after_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.db"
    original = SqliteBackend(path)
    assert original.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    ).inserted
    assert original.bind(
        key=KEY,
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ).bound

    reopened = SqliteBackend(path)
    assert reopened.get_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == (SCHEMA, CANONICAL)
    assert reopened.get_binding(key=KEY) == (SCHEMA, CONTENT_HASH)
    assert reopened.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    ) == PutOutcome(
        inserted=False,
        stored_schema=SCHEMA,
        stored_canonical=CANONICAL,
    )
    assert reopened.bind(
        key=KEY,
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == BindOutcome(
        bound=False,
        existing_schema=SCHEMA,
        existing_content_hash=CONTENT_HASH,
    )


def test_failed_transaction_rolls_back_and_connection_is_reusable(
    tmp_path: Path,
) -> None:
    backend = SqliteBackend(tmp_path / "store.db")
    connection = backend._conn

    with pytest.raises(RuntimeError, match="injected transaction failure"):
        _write_then_fail(backend)

    assert backend._conn is connection
    assert backend.get_binding(key=KEY) is None
    assert backend.bind(
        key=KEY,
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == BindOutcome(
        bound=True,
        existing_schema=SCHEMA,
        existing_content_hash=CONTENT_HASH,
    )
    assert backend.get_binding(key=KEY) == (SCHEMA, CONTENT_HASH)


def _write_then_fail(backend: SqliteBackend) -> None:
    with backend._immediate() as transaction:
        transaction.execute(
            "INSERT INTO bindings (key, schema, content_hash) "
            "VALUES (?, ?, ?)",
            (KEY, SCHEMA, CONTENT_HASH),
        )
        raise RuntimeError("injected transaction failure")


def _sqlite_contender(
    contender: Contender,
    messages: Connection,
    release: Event,
) -> None:
    db_path, operation, identity, value = contender
    backend = SqliteBackend(db_path)
    messages.send(("ready", identity))
    if not release.wait(WATCHDOG_SECONDS):
        messages.send(("watchdog", identity))
        return

    if operation == "put":
        put = backend.put_object(
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
            canonical=value,
        )
        messages.send(
            (
                "result",
                identity,
                put.inserted,
                put.stored_schema,
                put.stored_canonical,
            )
        )
        return

    bound = backend.bind(
        key=KEY,
        schema=f"schema.{value}",
        content_hash=value * 64,
    )
    messages.send(
        (
            "result",
            identity,
            bound.bound,
            bound.existing_schema,
            bound.existing_content_hash,
        )
    )


def _run_process_contenders(
    db_path: Path,
    operation: Operation,
    values: list[str],
) -> list[ProcessResult]:
    # Concurrent first initialization is outside this contention test's scope.
    SqliteBackend(db_path)
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    processes: list[BaseProcess] = []
    receivers: list[Connection] = []
    results: list[ProcessResult] = []
    stuck: list[int | None] = []

    for identity, value in enumerate(values):
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_sqlite_contender,
            args=(
                (str(db_path), operation, identity, value),
                sender,
                release,
            ),
        )
        receivers.append(receiver)
        processes.append(process)
        process.start()
        sender.close()

    try:
        for identity, receiver in enumerate(receivers):
            if not receiver.poll(WATCHDOG_SECONDS):
                raise AssertionError(
                    f"contender {identity} did not report ready"
                )
            assert receiver.recv() == ("ready", identity)

        release.set()
        for identity, receiver in enumerate(receivers):
            if not receiver.poll(WATCHDOG_SECONDS):
                raise AssertionError(
                    f"contender {identity} did not report an outcome"
                )
            message = receiver.recv()
            assert message[0] == "result", message
            results.append(message)
    finally:
        release.set()
        for process in processes:
            process.join(WATCHDOG_SECONDS)
            if process.is_alive():
                stuck.append(process.pid)
                process.terminate()
                process.join(WATCHDOG_SECONDS)
            process.close()
        for receiver in receivers:
            receiver.close()

    assert stuck == [], f"contenders exceeded teardown watchdog: {stuck}"
    return results


@pytest.mark.parametrize(
    "canonicals",
    [
        pytest.param([CANONICAL] * PROCESS_CONTENDERS, id="same-value"),
        pytest.param(
            [
                f'{{"contender":{index}}}'
                for index in range(PROCESS_CONTENDERS)
            ],
            id="competing-values",
        ),
    ],
)
def test_cross_process_put_has_one_reopened_correlated_winner(
    tmp_path: Path,
    canonicals: list[str],
) -> None:
    path = tmp_path / "store.db"
    results = _run_process_contenders(path, "put", canonicals)

    assert sum(result[2] for result in results) == 1
    reopened = SqliteBackend(path)
    winner = reopened.get_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    )
    assert winner is not None
    winner_schema, winner_canonical = winner
    assert winner_schema == SCHEMA
    assert winner_canonical in canonicals
    for _, identity, inserted, stored_schema, stored_canonical in results:
        assert stored_schema == winner_schema
        assert stored_canonical == winner_canonical
        if inserted:
            assert canonicals[identity] == winner_canonical


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(["a"] * PROCESS_CONTENDERS, id="same-reference"),
        pytest.param(
            [f"{index:x}" for index in range(PROCESS_CONTENDERS)],
            id="competing-references",
        ),
    ],
)
def test_cross_process_bind_has_one_reopened_correlated_winner(
    tmp_path: Path,
    values: list[str],
) -> None:
    path = tmp_path / "store.db"
    results = _run_process_contenders(path, "bind", values)

    assert sum(result[2] for result in results) == 1
    reopened = SqliteBackend(path)
    winner = reopened.get_binding(key=KEY)
    expected_references = [(f"schema.{value}", value * 64) for value in values]
    assert winner in expected_references
    for _, identity, bound, existing_schema, existing_hash in results:
        assert (existing_schema, existing_hash) == winner
        if bound:
            assert expected_references[identity] == winner
