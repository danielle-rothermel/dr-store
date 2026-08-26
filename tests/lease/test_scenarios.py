from __future__ import annotations

from datetime import timedelta

import pytest

from dr_store.content_addressing import ObjectReference
from dr_store.lease import (
    AcquireOutcome,
    LeaseAuthorityError,
    ReplayPolicy,
    StaleLeaseError,
    TerminalConflictError,
    TerminalFailure,
    TerminalOutcome,
)
from tests.lease.conftest import (
    LEASE_DURATION,
    REQUEST_HASH_B,
    AuthorityFixture,
    force_expire,
    make_request,
    set_fence,
)

RESULT_REF = ObjectReference(schema="demo.record", content_hash="c" * 64)
_MAX_FENCE = (1 << 63) - 1


def _acquire(
    fixture: AuthorityFixture,
    *,
    owner_id: str = "owner-a",
    attempt_id: str = "attempt-a",
    replay_policy: ReplayPolicy = ReplayPolicy.IDEMPOTENT,
    request_hash: str | None = None,
):
    request = make_request(
        semantic_key=fixture.semantic_key,
        replay_policy=replay_policy,
        request_hash=request_hash or make_request().request_hash,
    )
    return fixture.authority.acquire(
        request,
        owner_id=owner_id,
        attempt_id=attempt_id,
        lease_duration=LEASE_DURATION,
    )


def test_fresh_acquire_returns_lease(
    authority_fixture: AuthorityFixture,
) -> None:
    result = _acquire(authority_fixture)
    assert result.outcome is AcquireOutcome.ACQUIRED
    assert result.lease is not None
    assert result.lease.fence == 1


@pytest.mark.parametrize(
    "replay_policy",
    [
        ReplayPolicy.IDEMPOTENT,
        ReplayPolicy.DURABLE_WORKFLOW,
        ReplayPolicy.NO_REDRIVE,
    ],
)
def test_reacquire_same_owner_attempt_returns_existing_lease(
    authority_fixture: AuthorityFixture,
    replay_policy: ReplayPolicy,
) -> None:
    first = _acquire(authority_fixture, replay_policy=replay_policy)
    second = _acquire(authority_fixture, replay_policy=replay_policy)
    assert first.lease is not None
    assert second.outcome is AcquireOutcome.ACQUIRED
    assert second.lease == first.lease


def test_busy_when_other_owner_holds_unexpired_lease(
    authority_fixture: AuthorityFixture,
) -> None:
    first = _acquire(authority_fixture, owner_id="owner-a", attempt_id="a1")
    busy = _acquire(
        authority_fixture,
        owner_id="owner-b",
        attempt_id="b1",
    )
    assert first.lease is not None
    assert busy.outcome is AcquireOutcome.BUSY
    assert busy.busy_expires_at == first.lease.expires_at


def test_terminal_replay_returns_terminal_outcome(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None
    terminal = authority_fixture.authority.succeed(
        acquired.lease,
        result_ref=RESULT_REF,
    )
    replay = _acquire(authority_fixture)
    assert replay.outcome is AcquireOutcome.SUCCEEDED
    assert replay.terminal == terminal


def test_request_conflict_returns_existing_identity(
    authority_fixture: AuthorityFixture,
) -> None:
    _acquire(authority_fixture)
    conflict = _acquire(
        authority_fixture,
        request_hash=REQUEST_HASH_B,
    )
    assert conflict.outcome is AcquireOutcome.REQUEST_CONFLICT
    assert conflict.existing_request_hash == make_request().request_hash
    assert conflict.existing_replay_policy is ReplayPolicy.IDEMPOTENT


def test_expired_takeover_increments_fence(
    authority_fixture: AuthorityFixture,
) -> None:
    first = _acquire(
        authority_fixture,
        owner_id="owner-a",
        attempt_id="attempt-1",
    )
    assert first.lease is not None
    force_expire(authority_fixture, authority_fixture.semantic_key)
    second = _acquire(
        authority_fixture,
        owner_id="owner-b",
        attempt_id="attempt-2",
    )
    assert second.outcome is AcquireOutcome.ACQUIRED
    assert second.lease is not None
    assert second.lease.fence == first.lease.fence + 1
    assert second.lease.owner_id == "owner-b"


def test_no_redrive_recovery_on_expired_lease(
    authority_fixture: AuthorityFixture,
) -> None:
    first = _acquire(
        authority_fixture,
        replay_policy=ReplayPolicy.NO_REDRIVE,
    )
    assert first.lease is not None
    force_expire(authority_fixture, authority_fixture.semantic_key)
    recovery = _acquire(
        authority_fixture,
        replay_policy=ReplayPolicy.NO_REDRIVE,
    )
    assert recovery.outcome is AcquireOutcome.RECOVERY_REQUIRED
    assert recovery.terminal is not None
    assert recovery.terminal.outcome is TerminalOutcome.RECOVERY_REQUIRED
    assert recovery.terminal.failure is not None
    assert recovery.terminal.failure.code.startswith("effect-recovery:")


def test_renew_extends_expiry(authority_fixture: AuthorityFixture) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None
    if authority_fixture.clock is not None:
        authority_fixture.clock.advance(timedelta(seconds=10))
    renewed = authority_fixture.authority.renew(
        acquired.lease,
        lease_duration=LEASE_DURATION,
    )
    assert renewed.expires_at >= acquired.lease.expires_at


def test_renew_returns_current_when_not_extending(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None
    renewed = authority_fixture.authority.renew(
        acquired.lease,
        lease_duration=timedelta(seconds=1),
    )
    assert renewed == acquired.lease


def test_renew_stale_on_owner_mismatch(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None
    stale = acquired.lease.model_copy(update={"owner_id": "other-owner"})
    with pytest.raises(StaleLeaseError):
        authority_fixture.authority.renew(
            stale,
            lease_duration=LEASE_DURATION,
        )


def test_renew_distinguishes_absent_row_from_foreign_writer(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None

    absent = acquired.lease.model_copy(
        update={
            "request": acquired.lease.request.model_copy(
                update={"semantic_key": "test.semantic/never-acquired"}
            )
        }
    )
    with pytest.raises(StaleLeaseError, match="row no longer exists"):
        authority_fixture.authority.renew(
            absent,
            lease_duration=LEASE_DURATION,
        )

    foreign = acquired.lease.model_copy(
        update={
            "request": acquired.lease.request.model_copy(
                update={"request_hash": REQUEST_HASH_B}
            )
        }
    )
    with pytest.raises(StaleLeaseError, match="different request"):
        authority_fixture.authority.renew(
            foreign,
            lease_duration=LEASE_DURATION,
        )


def test_terminalize_distinguishes_absent_row_from_foreign_writer(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None

    absent = acquired.lease.model_copy(
        update={
            "request": acquired.lease.request.model_copy(
                update={"semantic_key": "test.semantic/never-acquired"}
            )
        }
    )
    with pytest.raises(StaleLeaseError, match="row no longer exists"):
        authority_fixture.authority.succeed(absent, result_ref=RESULT_REF)

    foreign = acquired.lease.model_copy(
        update={
            "request": acquired.lease.request.model_copy(
                update={"request_hash": REQUEST_HASH_B}
            )
        }
    )
    with pytest.raises(StaleLeaseError, match="different request"):
        authority_fixture.authority.succeed(foreign, result_ref=RESULT_REF)


def test_terminalize_is_idempotent(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None
    first = authority_fixture.authority.succeed(
        acquired.lease,
        result_ref=RESULT_REF,
    )
    second = authority_fixture.authority.succeed(
        acquired.lease,
        result_ref=RESULT_REF,
    )
    assert first == second


def test_terminalize_conflict_on_different_terminal(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None
    authority_fixture.authority.succeed(
        acquired.lease,
        result_ref=RESULT_REF,
    )
    with pytest.raises(TerminalConflictError):
        authority_fixture.authority.fail(
            acquired.lease,
            result_ref=RESULT_REF,
            failure=TerminalFailure(
                code="demo.failure",
                message="failed",
                details={},
            ),
        )


def test_terminalize_stale_after_expiry(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None
    force_expire(authority_fixture, authority_fixture.semantic_key)
    with pytest.raises(StaleLeaseError):
        authority_fixture.authority.succeed(
            acquired.lease,
            result_ref=RESULT_REF,
        )


def test_verify_terminal_round_trip(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None
    terminal = authority_fixture.authority.succeed(
        acquired.lease,
        result_ref=RESULT_REF,
    )
    assert authority_fixture.authority.verify_terminal(terminal) == terminal


def test_verify_terminal_rejects_non_authoritative(
    authority_fixture: AuthorityFixture,
) -> None:
    acquired = _acquire(authority_fixture)
    assert acquired.lease is not None
    terminal = authority_fixture.authority.succeed(
        acquired.lease,
        result_ref=RESULT_REF,
    )
    forged = terminal.model_copy(update={"fence": terminal.fence + 1})
    with pytest.raises(TerminalConflictError):
        authority_fixture.authority.verify_terminal(forged)


def test_fence_ceiling_raises(authority_fixture: AuthorityFixture) -> None:
    if authority_fixture.kind == "memory":
        request = make_request(semantic_key=authority_fixture.semantic_key)
        authority = authority_fixture.authority
        store = authority._store
        now = (
            authority_fixture.clock.now() if authority_fixture.clock else None
        )
        assert now is not None
        from dr_store.lease.models import _LeaseRow

        lease = authority.acquire(
            request,
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=LEASE_DURATION,
        ).lease
        assert lease is not None
        store._rows[authority_fixture.semantic_key] = _LeaseRow.leased(  # ty: ignore[unresolved-attribute]
            lease.model_copy(
                update={
                    "fence": _MAX_FENCE,
                    "expires_at": now - timedelta(seconds=1),
                }
            )
        )
        with pytest.raises(LeaseAuthorityError, match="fence exhausted"):
            authority.acquire(
                request,
                owner_id="owner-final",
                attempt_id="attempt-final",
                lease_duration=LEASE_DURATION,
            )
        return

    _acquire(authority_fixture)
    set_fence(
        authority_fixture, authority_fixture.semantic_key, fence=_MAX_FENCE
    )
    with pytest.raises(LeaseAuthorityError, match="fence exhausted"):
        _acquire(
            authority_fixture,
            owner_id="owner-final",
            attempt_id="attempt-final",
        )


def test_sqlite_minimum_duration_rejected(
    authority_fixture: AuthorityFixture,
) -> None:
    if authority_fixture.kind != "sqlite":
        pytest.skip("sqlite-only minimum duration rule")
    with pytest.raises(ValueError, match="1 millisecond"):
        authority_fixture.authority.acquire(
            make_request(),
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=timedelta(microseconds=999),
        )
