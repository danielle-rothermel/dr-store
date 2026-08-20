from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from dr_store.lease import (
    AcquireOutcome,
    LeaseAuthority,
    LeaseRequest,
    ReplayPolicy,
)
from tests.conftest import require_postgres_dsn

if TYPE_CHECKING:
    from collections.abc import Iterator

require_postgres_dsn(_module_level=True)


class _ConnectionTracker:
    def __init__(self) -> None:
        self.connect_calls = 0

    @contextmanager
    def connect(self, dsn: str) -> Iterator[Any]:
        from psycopg import connect

        self.connect_calls += 1
        with connect(dsn) as connection:
            yield connection


def test_postgres_authority_opens_fresh_connection_per_operation(
    postgres_dsn: str,
) -> None:
    tracker = _ConnectionTracker()
    semantic_key = f"test.connection/{uuid.uuid4().hex}"
    authority = LeaseAuthority.postgresql(
        postgres_dsn, _connect=tracker.connect
    )
    request = LeaseRequest(
        semantic_key=semantic_key,
        request_hash="d" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )
    try:
        init_calls = tracker.connect_calls
        authority.acquire(
            request,
            owner_id="owner",
            attempt_id="attempt-a",
            lease_duration=timedelta(seconds=30),
        )
        authority.acquire(
            request,
            owner_id="owner",
            attempt_id="attempt-b",
            lease_duration=timedelta(seconds=30),
        )
    finally:
        authority.close()

    assert tracker.connect_calls - init_calls == 2


def test_lease_survives_caller_transaction_rollback(postgres_dsn: str) -> None:
    from psycopg import connect

    semantic_key = f"test.rollback/{uuid.uuid4().hex}"
    request = LeaseRequest(
        semantic_key=semantic_key,
        request_hash="e" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )

    with connect(postgres_dsn) as caller:
        caller.execute("BEGIN")
        authority = LeaseAuthority.postgresql(postgres_dsn)
        try:
            acquired = authority.acquire(
                request,
                owner_id="owner",
                attempt_id="attempt",
                lease_duration=timedelta(seconds=30),
            )
        finally:
            authority.close()
        assert acquired.outcome is AcquireOutcome.ACQUIRED
        caller.execute("ROLLBACK")

    with connect(postgres_dsn) as observer:
        row = observer.execute(
            """
            SELECT state FROM dr_store_lease_authority
            WHERE semantic_key = %s
            """,
            (semantic_key,),
        ).fetchone()
    assert row is not None
    assert row[0] == "leased"
