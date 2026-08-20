from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from dr_store.lease import LeaseAuthority, LeaseRequest, ReplayPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator

if os.environ.get("DR_STORE_POSTGRES_DSN") is None:
    pytest.skip(
        "DR_STORE_POSTGRES_DSN is not configured",
        allow_module_level=True,
    )


class _ConnectionTracker:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.connection_ids: list[int] = []

    @contextmanager
    def connect(self, dsn: str) -> Iterator[Any]:
        from psycopg import connect

        self.connect_calls += 1
        with connect(dsn) as connection:
            self.connection_ids.append(self.connect_calls)
            yield connection


def test_postgres_authority_opens_fresh_connection_per_operation() -> None:
    dsn = os.environ["DR_STORE_POSTGRES_DSN"]
    tracker = _ConnectionTracker()
    semantic_key = f"test.connection/{uuid.uuid4().hex}"
    authority = LeaseAuthority.postgresql(dsn, _connect=tracker.connect)
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
    assert tracker.connection_ids[-1] != tracker.connection_ids[-2]
