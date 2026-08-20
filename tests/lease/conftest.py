from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from dr_store.lease import LeaseAuthority, LeaseRequest, ReplayPolicy
from dr_store.testing import FakeClock

if TYPE_CHECKING:
    from collections.abc import Iterator

LEASE_DURATION = timedelta(seconds=30)
REQUEST_HASH_A = "a" * 64
REQUEST_HASH_B = "b" * 64
SEMANTIC_KEY = "test.semantic/key"


@dataclass(frozen=True, slots=True)
class AuthorityFixture:
    authority: LeaseAuthority
    kind: str
    semantic_key: str
    clock: FakeClock | None = None
    sqlite_path: Path | None = None


def make_request(
    *,
    semantic_key: str = SEMANTIC_KEY,
    request_hash: str = REQUEST_HASH_A,
    replay_policy: ReplayPolicy = ReplayPolicy.IDEMPOTENT,
) -> LeaseRequest:
    return LeaseRequest(
        semantic_key=semantic_key,
        request_hash=request_hash,
        replay_policy=replay_policy,
    )


def set_fence(
    authority_fixture: AuthorityFixture,
    semantic_key: str,
    *,
    fence: int,
) -> None:
    if authority_fixture.kind == "memory":
        raise AssertionError("memory backend cannot set fence directly")
    expired = datetime(2000, 1, 1, tzinfo=UTC).isoformat(
        timespec="microseconds"
    )
    if authority_fixture.kind == "sqlite":
        if authority_fixture.sqlite_path is None:
            raise AssertionError("sqlite fixture requires path")
        connection = sqlite3.connect(authority_fixture.sqlite_path)
        try:
            connection.execute(
                """
                UPDATE dr_store_lease_authority
                SET fence = ?, expires_at = ?
                WHERE semantic_key = ?
                """,
                (fence, expired, semantic_key),
            )
            connection.commit()
        finally:
            connection.close()
        return
    dsn = os.environ.get("DR_STORE_POSTGRES_DSN")
    if dsn is None:
        raise AssertionError("postgres fixture requires DSN")
    from psycopg import connect

    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
                UPDATE dr_store_lease_authority
                SET fence = %s, expires_at = %s
                WHERE semantic_key = %s
                """,
            (fence, expired, semantic_key),
        )


def force_expire(
    authority_fixture: AuthorityFixture, semantic_key: str
) -> None:
    expired = datetime(2000, 1, 1, tzinfo=UTC).isoformat(
        timespec="microseconds"
    )
    if authority_fixture.kind == "memory":
        if authority_fixture.clock is None:
            raise AssertionError("memory fixture requires FakeClock")
        authority_fixture.clock.advance(timedelta(days=365))
        return
    if authority_fixture.kind == "sqlite":
        if authority_fixture.sqlite_path is None:
            raise AssertionError("sqlite fixture requires path")
        connection = sqlite3.connect(authority_fixture.sqlite_path)
        try:
            connection.execute(
                """
                UPDATE dr_store_lease_authority
                SET expires_at = ?
                WHERE semantic_key = ?
                """,
                (expired, semantic_key),
            )
            connection.commit()
        finally:
            connection.close()
        return
    dsn = os.environ.get("DR_STORE_POSTGRES_DSN")
    if dsn is None:
        raise AssertionError("postgres fixture requires DSN")
    from psycopg import connect

    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
                UPDATE dr_store_lease_authority
                SET expires_at = %s
                WHERE semantic_key = %s
                """,
            (expired, semantic_key),
        )


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def authority_fixture(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterator[AuthorityFixture]:
    kind = request.param
    if kind == "memory":
        clock = FakeClock()
        authority = LeaseAuthority.memory(clock=clock.now)
        fixture = AuthorityFixture(
            authority=authority,
            kind=kind,
            semantic_key=SEMANTIC_KEY,
            clock=clock,
        )
    elif kind == "sqlite":
        path = tmp_path / "lease.sqlite3"
        authority = LeaseAuthority.sqlite(path)
        fixture = AuthorityFixture(
            authority=authority,
            kind=kind,
            semantic_key=SEMANTIC_KEY,
            sqlite_path=path,
        )
    else:
        dsn = os.environ.get("DR_STORE_POSTGRES_DSN")
        if dsn is None:
            if os.environ.get("DR_STORE_REQUIRE_POSTGRES") == "1":
                pytest.fail(
                    "DR_STORE_REQUIRE_POSTGRES=1 requires "
                    "DR_STORE_POSTGRES_DSN"
                )
            pytest.skip("DR_STORE_POSTGRES_DSN is not configured")
        authority = LeaseAuthority.postgresql(dsn)
        fixture = AuthorityFixture(
            authority=authority,
            kind=kind,
            semantic_key=f"test.semantic/{uuid.uuid4().hex}",
        )
    try:
        yield fixture
    finally:
        authority.close()
