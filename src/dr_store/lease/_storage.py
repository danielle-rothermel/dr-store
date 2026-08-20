from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol, TypeVar

from dr_serialize import decode_strict_json_bytes

from dr_store.lease.models import (
    LeaseRequest,
    ReplayPolicy,
    Terminal,
    _AuthorityCorruptionError,
    _LeaseRow,
    _require_utc,
    _StoredState,
)
from dr_store.relational._helpers import (
    require_persisted_integer as _relational_require_persisted_integer,
)
from dr_store.relational._helpers import (
    require_persisted_text as _relational_require_persisted_text,
)
from dr_store.relational.errors import RelationalContractMismatchError

_T = TypeVar("_T")
_Transition = Callable[
    [_LeaseRow | None, datetime], tuple[_LeaseRow | None, _T]
]


class _Store(Protocol):
    def initialize(self) -> None: ...

    def validate_lease_duration(self, duration: timedelta) -> timedelta: ...

    def transaction(
        self,
        semantic_key: str,
        transition: _Transition[_T],
    ) -> _T: ...

    def close(self) -> None: ...


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    _require_utc(value, field="persisted timestamp")
    return value.isoformat(timespec="microseconds")


def _terminal_text(value: Terminal | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _row_insert_values(row: _LeaseRow) -> tuple[Any, ...]:
    return (
        str(row.request.semantic_key),
        str(row.request.request_hash),
        row.request.replay_policy.value,
        row.state.value,
        str(row.owner_id),
        str(row.attempt_id),
        row.fence,
        _timestamp_text(row.expires_at),
        _terminal_text(row.terminal),
    )


def _row_update_values(row: _LeaseRow) -> tuple[Any, ...]:
    return _row_insert_values(row)[1:]


def _row_match_values(row: _LeaseRow) -> tuple[Any, ...]:
    return (
        str(row.request.request_hash),
        row.request.replay_policy.value,
        row.state.value,
        str(row.owner_id),
        str(row.attempt_id),
        row.fence,
        _timestamp_text(row.expires_at),
        _terminal_text(row.terminal),
    )


def _require_persisted_text(value: object, *, field: str) -> str:
    try:
        return _relational_require_persisted_text(value, field=field)
    except RelationalContractMismatchError as exc:
        raise _AuthorityCorruptionError(
            f"persisted {field} must have text storage"
        ) from exc


def _require_persisted_integer(value: object, *, field: str) -> int:
    try:
        return _relational_require_persisted_integer(value, field=field)
    except RelationalContractMismatchError as exc:
        raise _AuthorityCorruptionError(
            f"persisted {field} must have integer storage"
        ) from exc


def _decode_row(
    semantic_key: str,
    raw: tuple[Any, ...] | None,
) -> _LeaseRow | None:
    if raw is None:
        return None
    (
        request_hash,
        replay_policy,
        state,
        owner_id,
        attempt_id,
        fence,
        expires_at,
        terminal_json,
    ) = raw
    request_hash_text = _require_persisted_text(
        request_hash,
        field="request_hash",
    )
    replay_policy_text = _require_persisted_text(
        replay_policy, field="replay_policy"
    )
    state_text = _require_persisted_text(state, field="state")
    owner_id_text = _require_persisted_text(owner_id, field="owner_id")
    attempt_id_text = _require_persisted_text(attempt_id, field="attempt_id")
    fence_integer = _require_persisted_integer(fence, field="fence")
    expires_at_text = (
        None
        if expires_at is None
        else _require_persisted_text(expires_at, field="expires_at")
    )
    terminal_text = (
        None
        if terminal_json is None
        else _require_persisted_text(terminal_json, field="terminal_json")
    )
    request = LeaseRequest.model_validate(
        {
            "semantic_key": semantic_key,
            "request_hash": request_hash_text,
            "replay_policy": ReplayPolicy(replay_policy_text),
        }
    )
    parsed_expires_at = (
        None
        if expires_at_text is None
        else datetime.fromisoformat(expires_at_text)
    )
    terminal = None
    if terminal_text is not None:
        terminal_raw = terminal_text.encode()
        decode_strict_json_bytes(
            terminal_raw,
            max_bytes=len(terminal_raw),
            max_depth=len(terminal_raw),
        )
        terminal = Terminal.model_validate_json(terminal_raw)
    row = _LeaseRow(
        request=request,
        state=_StoredState(state_text),
        owner_id=owner_id_text,
        attempt_id=attempt_id_text,
        fence=fence_integer,
        expires_at=parsed_expires_at,
        terminal=terminal,
    )
    if row.state is _StoredState.LEASED:
        if terminal is not None:
            raise _AuthorityCorruptionError(
                "persisted lease contains a terminal record"
            )
        row.lease()
    else:
        if row.expires_at is not None:
            raise _AuthorityCorruptionError(
                "persisted terminal contains a lease expiration"
            )
        if terminal is None or terminal.outcome.value != row.state.value:
            raise _AuthorityCorruptionError(
                "persisted terminal state and record disagree"
            )
    if terminal is not None and (
        terminal.request != request
        or terminal.owner_id != row.owner_id
        or terminal.attempt_id != row.attempt_id
        or terminal.fence != row.fence
    ):
        raise _AuthorityCorruptionError(
            "persisted terminal metadata and row disagree"
        )
    return row
