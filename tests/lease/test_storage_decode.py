from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dr_store.content_addressing import ObjectReference
from dr_store.lease import (
    LeaseRequest,
    ReplayPolicy,
    Terminal,
    TerminalOutcome,
)
from dr_store.lease._storage import _decode_row
from dr_store.lease.models import _AuthorityCorruptionError, _StoredState

_SEMANTIC_KEY = "test.semantic/key"
_REQUEST = LeaseRequest(
    semantic_key=_SEMANTIC_KEY,
    request_hash="a" * 64,
    replay_policy=ReplayPolicy.IDEMPOTENT,
)
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_RESULT_REF = ObjectReference(schema="demo.record", content_hash="c" * 64)
_TERMINAL = Terminal(
    request=_REQUEST,
    outcome=TerminalOutcome.SUCCEEDED,
    owner_id="owner",
    attempt_id="attempt",
    fence=1,
    result_ref=_RESULT_REF,
)
_TERMINAL_JSON = _TERMINAL.model_dump_json()


def _leased_raw(*, terminal_json: str | None = None) -> tuple[object, ...]:
    return (
        _REQUEST.request_hash,
        ReplayPolicy.IDEMPOTENT.value,
        _StoredState.LEASED.value,
        "owner",
        "attempt",
        1,
        _NOW.isoformat(timespec="microseconds"),
        terminal_json,
    )


def _terminal_raw(  # noqa: PLR0913
    *,
    state: str = _StoredState.SUCCEEDED.value,
    expires_at: str | None = None,
    terminal_json: str | None = _TERMINAL_JSON,
    owner_id: str = "owner",
    attempt_id: str = "attempt",
    fence: int = 1,
) -> tuple[object, ...]:
    return (
        _REQUEST.request_hash,
        ReplayPolicy.IDEMPOTENT.value,
        state,
        owner_id,
        attempt_id,
        fence,
        expires_at,
        terminal_json,
    )


def test_decode_rejects_leased_row_with_terminal_payload() -> None:
    with pytest.raises(_AuthorityCorruptionError, match="terminal record"):
        _decode_row(_SEMANTIC_KEY, _leased_raw(terminal_json=_TERMINAL_JSON))


def test_decode_rejects_terminal_row_with_expiration() -> None:
    with pytest.raises(_AuthorityCorruptionError, match="lease expiration"):
        _decode_row(
            _SEMANTIC_KEY,
            _terminal_raw(expires_at=_NOW.isoformat(timespec="microseconds")),
        )


def test_decode_rejects_terminal_row_without_terminal_payload() -> None:
    with pytest.raises(_AuthorityCorruptionError, match="disagree"):
        _decode_row(_SEMANTIC_KEY, _terminal_raw(terminal_json=None))


def test_decode_rejects_terminal_outcome_mismatch() -> None:
    with pytest.raises(_AuthorityCorruptionError, match="disagree"):
        _decode_row(
            _SEMANTIC_KEY, _terminal_raw(state=_StoredState.FAILED.value)
        )


def test_decode_rejects_terminal_owner_mismatch() -> None:
    with pytest.raises(_AuthorityCorruptionError, match="metadata and row"):
        _decode_row(_SEMANTIC_KEY, _terminal_raw(owner_id="other-owner"))


def test_decode_rejects_terminal_attempt_mismatch() -> None:
    with pytest.raises(_AuthorityCorruptionError, match="metadata and row"):
        _decode_row(_SEMANTIC_KEY, _terminal_raw(attempt_id="other-attempt"))


def test_decode_accepts_valid_terminal_row() -> None:
    row = _decode_row(_SEMANTIC_KEY, _terminal_raw())
    assert row is not None
    assert row.state is _StoredState.SUCCEEDED
    assert row.terminal is not None
    assert row.terminal.outcome is TerminalOutcome.SUCCEEDED


def test_decode_type_guard_maps_to_authority_corruption() -> None:
    with pytest.raises(_AuthorityCorruptionError, match="text storage"):
        _decode_row(
            _SEMANTIC_KEY,
            (
                1,
                ReplayPolicy.IDEMPOTENT.value,
                _StoredState.LEASED.value,
                "owner",
                "attempt",
                1,
                _NOW.isoformat(timespec="microseconds"),
                None,
            ),
        )
