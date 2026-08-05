from __future__ import annotations

import multiprocessing
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
