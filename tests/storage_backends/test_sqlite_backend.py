from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
import threading
from typing import TYPE_CHECKING

import pytest

from dr_store import SqliteBackend

if TYPE_CHECKING:
    from multiprocessing.queues import Queue
    from multiprocessing.synchronize import Event
    from pathlib import Path

SCHEMA = "example.record"
CONTENT_HASH = "a" * 64
CANONICAL = '{"value":"stored"}'
KEY = "durable-key"
WATCHDOG_SECONDS = 15


async def _thread_event(event: threading.Event) -> None:
    assert await asyncio.wait_for(
        asyncio.to_thread(event.wait), WATCHDOG_SECONDS
    )


def test_direct_construction_is_private(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match=r"await SqliteBackend\.open"):
        SqliteBackend(tmp_path / "store.db")


@pytest.mark.parametrize("path", ["", ":memory:"])
async def test_open_rejects_transient_paths(path: str) -> None:
    with pytest.raises(ValueError, match="persistent filesystem path"):
        await SqliteBackend.open(path)


async def test_open_captures_relative_path_and_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    backend = await SqliteBackend.open("store.db")
    monkeypatch.chdir(second)
    try:
        assert (
            await backend.bind(
                key=KEY, schema=SCHEMA, content_hash=CONTENT_HASH
            )
        ).bound
    finally:
        await backend.aclose()
    assert (first / "store.db").exists()
    assert not (second / "store.db").exists()


async def test_wal_normal_and_hash_lookup_plan_upgrades_existing_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE objects (
            schema TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            canonical TEXT NOT NULL,
            PRIMARY KEY (schema, content_hash)
        ) WITHOUT ROWID;
        CREATE TABLE bindings (
            key TEXT PRIMARY KEY NOT NULL,
            schema TEXT NOT NULL,
            content_hash TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )
    connection.close()

    backend = await SqliteBackend.open(path)
    try:
        journal_mode = await backend._run(
            lambda: backend._connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
        )
        synchronous = await backend._run(
            lambda: backend._connection.execute(
                "PRAGMA synchronous"
            ).fetchone()[0]
        )
        plan = await backend._run(
            lambda: backend._connection.execute(
                "EXPLAIN QUERY PLAN SELECT schema, canonical FROM objects "
                "WHERE content_hash = ? "
                "ORDER BY schema = ? DESC LIMIT 1",
                (CONTENT_HASH, SCHEMA),
            ).fetchall()
        )
    finally:
        await backend.aclose()

    assert journal_mode == "wal"
    assert synchronous == 1  # SQLite's numeric value for NORMAL.
    details = [row[3] for row in plan]
    assert any("objects_by_content_hash" in detail for detail in details)
    assert not any("SCAN objects" in detail for detail in details)


async def test_rows_persist_after_terminal_close_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.db"
    first = await SqliteBackend.open(path)
    assert (
        await first.put_object(
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
            canonical=CANONICAL,
        )
    ).inserted
    assert (
        await first.bind(key=KEY, schema=SCHEMA, content_hash=CONTENT_HASH)
    ).bound
    await first.aclose()
    await first.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        await first.get_binding(key="\0")

    reopened = await SqliteBackend.open(path)
    try:
        assert await reopened.get_object(
            schema=SCHEMA, content_hash=CONTENT_HASH
        ) == (SCHEMA, CANONICAL)
        assert await reopened.get_binding(key=KEY) == (SCHEMA, CONTENT_HASH)
    finally:
        await reopened.aclose()


async def test_admission_cancellation_submits_no_second_worker_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = await SqliteBackend.open(tmp_path / "store.db")
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def gated_get(key: str) -> tuple[str, str] | None:
        calls.append(key)
        started.set()
        assert release.wait(WATCHDOG_SECONDS)
        return None

    monkeypatch.setattr(backend, "_get_binding", gated_get)
    admitted = asyncio.create_task(backend.get_binding(key="first"))
    await _thread_event(started)
    waiting = asyncio.create_task(backend.get_binding(key="second"))
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    assert await admitted is None
    assert calls == ["first"]
    await backend.aclose()


async def test_cancelled_admitted_operation_settles_and_failure_is_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = await SqliteBackend.open(tmp_path / "store.db")
    started = threading.Event()
    release = threading.Event()
    worker_failure = RuntimeError("injected worker failure")

    def fail_after_release(_key: str) -> None:
        started.set()
        assert release.wait(WATCHDOG_SECONDS)
        raise worker_failure

    monkeypatch.setattr(backend, "_get_binding", fail_after_release)
    operation = asyncio.create_task(backend.get_binding(key=KEY))
    await _thread_event(started)
    operation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await operation
    assert caught.value.__cause__ is worker_failure

    monkeypatch.undo()
    assert await backend.get_binding(key=KEY) is None
    await backend.aclose()


async def test_close_waits_for_admitted_work_and_rejects_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = await SqliteBackend.open(tmp_path / "store.db")
    started = threading.Event()
    release = threading.Event()
    close_started = asyncio.Event()
    close_resources = backend._close_resources

    def gated_get(_key: str) -> None:
        started.set()
        assert release.wait(WATCHDOG_SECONDS)

    async def observed_close() -> None:
        close_started.set()
        await close_resources()

    monkeypatch.setattr(backend, "_get_binding", gated_get)
    monkeypatch.setattr(backend, "_close_resources", observed_close)
    operation = asyncio.create_task(backend.get_binding(key=KEY))
    await _thread_event(started)
    closing = asyncio.create_task(backend.aclose())
    await asyncio.wait_for(close_started.wait(), WATCHDOG_SECONDS)
    with pytest.raises(RuntimeError, match="closed"):
        await backend.get_binding(key="new")
    release.set()
    assert await operation is None
    await closing


async def test_cross_loop_use_fails_visibly(tmp_path: Path) -> None:
    backend = await SqliteBackend.open(tmp_path / "store.db")

    def other_loop() -> BaseException:
        try:
            asyncio.run(backend.get_binding(key=KEY))
        except BaseException as error:  # noqa: BLE001 - inspect boundary.
            return error
        raise AssertionError("cross-loop operation unexpectedly succeeded")

    error = await asyncio.to_thread(other_loop)
    assert isinstance(error, RuntimeError)
    assert "event loop" in str(error)
    await backend.aclose()


def _process_bind(
    path: str,
    identity: int,
    release: Event,
    results: Queue[tuple[bool, str, str]],
) -> None:
    async def run() -> None:
        backend = await SqliteBackend.open(path)
        assert release.wait(WATCHDOG_SECONDS)
        outcome = await backend.bind(
            key=KEY,
            schema=f"schema.{identity}",
            content_hash=f"{identity:x}" * 64,
        )
        results.put(
            (
                outcome.bound,
                outcome.existing_schema,
                outcome.existing_content_hash,
            )
        )
        await backend.aclose()

    asyncio.run(run())


async def test_cross_process_contention_has_one_correlated_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.db"
    initialized = await SqliteBackend.open(path)
    await initialized.aclose()
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_bind,
            args=(str(path), identity, release, results),
        )
        for identity in range(4)
    ]
    for process in processes:
        process.start()
    release.set()
    observed = [
        await asyncio.wait_for(
            asyncio.to_thread(results.get), WATCHDOG_SECONDS
        )
        for _ in processes
    ]
    for process in processes:
        await asyncio.to_thread(process.join, WATCHDOG_SECONDS)
        assert not process.is_alive()
        assert process.exitcode == 0
        process.close()
    results.close()
    results.join_thread()
    assert sum(bound for bound, _, _ in observed) == 1
    winners = {(schema, content_hash) for _, schema, content_hash in observed}
    assert len(winners) == 1
