"""Storage backend concurrency produces exactly one binding winner.

Every loser must observe either idempotent same-reference success or an
explicit different-reference conflict -- never a second successful bind and
never a silent overwrite. This is proven both with threads (both backends)
and across real processes (the durable SQLite backend).
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import TYPE_CHECKING

from dr_store import (
    BindingConflictError,
    BindStatus,
    ObjectReference,
    ObjectStore,
    SqliteBackend,
)

if TYPE_CHECKING:
    from pathlib import Path

KEY = "contended-key"
WINNER = ObjectReference.for_record("example.record", {"who": "winner"})
CONTENDERS = 32


def test_parallel_same_reference_binds_one_winner_rest_idempotent(
    store: ObjectStore,
) -> None:
    start = threading.Barrier(CONTENDERS)
    statuses: list[BindStatus] = []
    lock = threading.Lock()

    def worker() -> None:
        start.wait()
        status = store.bind(KEY, WINNER)
        with lock:
            statuses.append(status)

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONTENDERS) as pool:
        futures = [pool.submit(worker) for _ in range(CONTENDERS)]
        for future in futures:
            future.result()

    assert statuses.count(BindStatus.BOUND) == 1
    assert statuses.count(BindStatus.IDEMPOTENT) == CONTENDERS - 1
    assert store.resolve(KEY) == WINNER


def test_parallel_different_references_one_winner_losers_conflict(
    store: ObjectStore,
) -> None:
    refs = [
        ObjectReference.for_record("example.record", {"who": i})
        for i in range(CONTENDERS)
    ]
    start = threading.Barrier(CONTENDERS)
    bound = 0
    conflicts = 0
    lock = threading.Lock()

    def worker(ref: ObjectReference) -> None:
        nonlocal bound, conflicts
        start.wait()
        try:
            status = store.bind(KEY, ref)
        except BindingConflictError:
            with lock:
                conflicts += 1
        else:
            assert status is BindStatus.BOUND
            with lock:
                bound += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONTENDERS) as pool:
        futures = [pool.submit(worker, ref) for ref in refs]
        for future in futures:
            future.result()

    assert bound == 1
    assert conflicts == CONTENDERS - 1
    winner = store.resolve(KEY)
    assert winner in refs


def _bind_in_subprocess(db_path: str, which: int) -> str:
    # Top-level so it is picklable for ProcessPoolExecutor spawn.
    from dr_store import (
        BindingConflictError,
        BindStatus,
        ObjectReference,
        ObjectStore,
        SqliteBackend,
    )

    store = ObjectStore(SqliteBackend(db_path))
    ref = ObjectReference.for_record("example.record", {"who": which})
    try:
        status = store.bind("cross-process-key", ref)
    except BindingConflictError:
        return "conflict"
    return "bound" if status is BindStatus.BOUND else "idempotent"


def test_cross_process_binds_one_durable_winner(tmp_path: Path) -> None:
    db_path = str(tmp_path / "store.db")
    # Initialize the schema once in the parent before forking contenders.
    SqliteBackend(db_path)
    workers = 8

    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                _bind_in_subprocess,
                [db_path] * workers,
                range(workers),
            )
        )

    assert results.count("bound") == 1
    assert results.count("conflict") == workers - 1
    assert "idempotent" not in results

    winner = ObjectStore(SqliteBackend(db_path)).resolve("cross-process-key")
    assert winner is not None


def test_cross_process_same_reference_is_idempotent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "store.db")
    # Initialize the schema once in the parent before forking contenders.
    SqliteBackend(db_path)
    workers = 8

    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                _bind_in_subprocess,
                [db_path] * workers,
                [0] * workers,
            )
        )

    assert results.count("bound") == 1
    assert results.count("idempotent") == workers - 1
    assert "conflict" not in results
