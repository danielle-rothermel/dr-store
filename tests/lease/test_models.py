from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dr_store.content_addressing import ObjectReference
from dr_store.lease import (
    AcquireOutcome,
    AcquireResult,
    Lease,
    LeaseRequest,
    ReplayPolicy,
    Terminal,
    TerminalFailure,
    TerminalOutcome,
)


def test_lease_request_rejects_empty_semantic_key() -> None:
    with pytest.raises(ValidationError):
        LeaseRequest(
            semantic_key="   ",
            request_hash="a" * 64,
            replay_policy=ReplayPolicy.IDEMPOTENT,
        )


def test_lease_request_rejects_invalid_request_hash() -> None:
    with pytest.raises(ValidationError):
        LeaseRequest(
            semantic_key="key",
            request_hash="not-a-hash",
            replay_policy=ReplayPolicy.IDEMPOTENT,
        )


def test_lease_request_rejects_nul_in_semantic_key() -> None:
    with pytest.raises(ValidationError):
        LeaseRequest(
            semantic_key="a\x00b",
            request_hash="a" * 64,
            replay_policy=ReplayPolicy.IDEMPOTENT,
        )


def test_lease_request_rejects_surrogate_in_semantic_key() -> None:
    with pytest.raises(ValidationError):
        LeaseRequest(
            semantic_key="\ud800",
            request_hash="a" * 64,
            replay_policy=ReplayPolicy.IDEMPOTENT,
        )


def test_lease_rejects_surrogate_in_owner_id() -> None:
    request = LeaseRequest(
        semantic_key="key",
        request_hash="a" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )
    with pytest.raises(ValidationError):
        Lease(
            request=request,
            owner_id="\ud800",
            attempt_id="attempt",
            fence=1,
            expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_lease_rejects_surrogate_in_attempt_id() -> None:
    request = LeaseRequest(
        semantic_key="key",
        request_hash="a" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )
    with pytest.raises(ValidationError):
        Lease(
            request=request,
            owner_id="owner",
            attempt_id="\ud800",
            fence=1,
            expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_terminal_failure_rejects_surrogate_in_code() -> None:
    with pytest.raises(ValidationError):
        TerminalFailure(
            code="\ud800",
            message="failed",
            details={},
        )


def test_terminal_failure_rejects_surrogate_in_message() -> None:
    with pytest.raises(ValidationError):
        TerminalFailure(
            code="demo.failure",
            message="\ud800",
            details={},
        )


def test_lease_requires_utc_expires_at() -> None:
    request = LeaseRequest(
        semantic_key="key",
        request_hash="a" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )
    with pytest.raises(ValidationError):
        Lease(
            request=request,
            owner_id="owner",
            attempt_id="attempt",
            fence=1,
            expires_at=datetime(2026, 1, 1),
        )


def test_lease_rejects_invalid_fence() -> None:
    request = LeaseRequest(
        semantic_key="key",
        request_hash="a" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )
    with pytest.raises(ValidationError):
        Lease(
            request=request,
            owner_id="owner",
            attempt_id="attempt",
            fence=0,
            expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_terminal_succeeded_requires_result_ref_only() -> None:
    request = LeaseRequest(
        semantic_key="key",
        request_hash="a" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )
    result_ref = ObjectReference(schema="demo.record", content_hash="b" * 64)
    with pytest.raises(ValidationError):
        Terminal(
            request=request,
            outcome=TerminalOutcome.SUCCEEDED,
            owner_id="owner",
            attempt_id="attempt",
            fence=1,
        )
    terminal = Terminal(
        request=request,
        outcome=TerminalOutcome.SUCCEEDED,
        owner_id="owner",
        attempt_id="attempt",
        fence=1,
        result_ref=result_ref,
    )
    assert terminal.result_ref == result_ref


def test_terminal_failed_requires_result_ref_and_failure() -> None:
    request = LeaseRequest(
        semantic_key="key",
        request_hash="a" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )
    result_ref = ObjectReference(schema="demo.record", content_hash="b" * 64)
    failure = TerminalFailure(
        code="demo.failure",
        message="failed",
        details={"reason": "test"},
    )
    terminal = Terminal(
        request=request,
        outcome=TerminalOutcome.FAILED,
        owner_id="owner",
        attempt_id="attempt",
        fence=1,
        result_ref=result_ref,
        failure=failure,
    )
    assert terminal.failure == failure


def test_acquire_result_population_rules() -> None:
    request = LeaseRequest(
        semantic_key="key",
        request_hash="a" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )
    lease = Lease(
        request=request,
        owner_id="owner",
        attempt_id="attempt",
        fence=1,
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ValidationError):
        AcquireResult(
            request=request,
            outcome=AcquireOutcome.ACQUIRED,
            lease=lease,
            terminal=Terminal(
                request=request,
                outcome=TerminalOutcome.SUCCEEDED,
                owner_id="owner",
                attempt_id="attempt",
                fence=1,
                result_ref=ObjectReference(
                    schema="demo.record",
                    content_hash="c" * 64,
                ),
            ),
        )


def test_terminal_failure_rejects_non_object_details() -> None:
    with pytest.raises(ValidationError):
        TerminalFailure.model_validate(
            {
                "code": "code",
                "message": "message",
                "details": ["not", "an", "object"],
            }
        )
