from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from dr_store.content_addressing import ObjectReference
from dr_store.lease import (
    AcquireOutcome,
    LeaseAuthority,
    ReplayPolicy,
    TerminalOutcome,
)
from dr_store.lease._storage import _terminal_text
from dr_store.lease.authority import _recovery_failure
from dr_store.lease.models import _LeaseRow
from tests.lease.conftest import (
    SEMANTIC_KEY,
    AuthorityFixture,
    force_expire,
    make_request,
)

RESULT_REF = ObjectReference(schema="demo.record", content_hash="f" * 64)


def test_persisted_terminal_json_shape(tmp_path) -> None:
    path = tmp_path / "lease.sqlite3"
    authority = LeaseAuthority.sqlite(path)
    try:
        request = make_request()
        acquired = authority.acquire(
            request,
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=timedelta(seconds=30),
        )
        assert acquired.lease is not None
        terminal = authority.succeed(
            acquired.lease,
            result_ref=RESULT_REF,
        )
        persisted = _terminal_text(terminal)
        assert persisted is not None
        payload = json.loads(persisted)
        assert payload["result_ref"] == {
            "schema": RESULT_REF.schema,
            "content_hash": RESULT_REF.content_hash,
        }
        assert payload["outcome"] == TerminalOutcome.SUCCEEDED.value
    finally:
        authority.close()


def test_recovery_failure_code_format() -> None:
    from dr_store.lease import Lease

    request = make_request(replay_policy=ReplayPolicy.NO_REDRIVE)
    expires_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    lease = Lease(
        request=request,
        owner_id="owner",
        attempt_id="attempt",
        fence=1,
        expires_at=expires_at,
    )
    row = _LeaseRow.leased(lease)
    failure = _recovery_failure(row)
    payload = {
        "semantic_key": request.semantic_key,
        "request_hash": request.request_hash,
        "owner_id": "owner",
        "attempt_id": "attempt",
        "fence": 1,
        "expires_at": expires_at.isoformat(timespec="microseconds"),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert failure.code == f"effect-recovery:{digest}"
    assert failure.details == payload


def test_recovery_failure_matches_authority(tmp_path) -> None:
    path = tmp_path / "lease.sqlite3"
    authority = LeaseAuthority.sqlite(path)
    try:
        request = make_request(replay_policy=ReplayPolicy.NO_REDRIVE)
        acquired = authority.acquire(
            request,
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=timedelta(seconds=30),
        )
        assert acquired.lease is not None
        force_expire(
            AuthorityFixture(
                authority=authority,
                kind="sqlite",
                semantic_key=SEMANTIC_KEY,
                sqlite_path=path,
            ),
            SEMANTIC_KEY,
        )
        recovery = authority.acquire(
            request,
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=timedelta(seconds=30),
        )
        assert recovery.outcome is AcquireOutcome.RECOVERY_REQUIRED
        assert recovery.terminal is not None
        assert recovery.terminal.failure is not None
        assert recovery.terminal.failure.code.startswith("effect-recovery:")
        connection = sqlite3.connect(path)
        try:
            raw = connection.execute(
                """
                SELECT terminal_json FROM dr_store_lease_authority
                WHERE semantic_key = ?
                """,
                (SEMANTIC_KEY,),
            ).fetchone()
        finally:
            connection.close()
        assert raw is not None
        persisted = json.loads(raw[0])
        assert persisted["failure"]["code"] == recovery.terminal.failure.code
    finally:
        authority.close()
