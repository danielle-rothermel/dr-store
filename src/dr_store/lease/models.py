from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import UNIQUE, StrEnum, verify
from typing import Any

from dr_serialize import validate_strict_json
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    field_validator,
    model_validator,
)

from dr_store.content_addressing import ObjectReference

_MAX_FENCE = (1 << 63) - 1
_HEX = frozenset("0123456789abcdef")


def _require_text(value: str, *, field: str, maximum: int = 1024) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    if "\x00" in value:
        raise ValueError(f"{field} cannot contain NUL")
    if len(value) > maximum:
        raise ValueError(f"{field} cannot exceed {maximum} characters")
    return value


def _require_request_hash(value: str) -> str:
    if len(value) != 64 or any(char not in _HEX for char in value):
        raise ValueError(
            "request_hash must be a full 64-char lowercase SHA-256 hash, "
            f"got {value!r}"
        )
    return value


def _require_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field} must use UTC")
    return value


def _require_lease_duration(value: timedelta) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError("lease_duration must be a timedelta")
    seconds = value.total_seconds()
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("lease_duration must be positive")
    return value


@verify(UNIQUE)
class ReplayPolicy(StrEnum):
    IDEMPOTENT = "idempotent"
    DURABLE_WORKFLOW = "durable_workflow"
    NO_REDRIVE = "no_redrive"


@verify(UNIQUE)
class TerminalOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


@verify(UNIQUE)
class AcquireOutcome(StrEnum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REQUEST_CONFLICT = "request_conflict"
    RECOVERY_REQUIRED = "recovery_required"


@verify(UNIQUE)
class _StoredState(StrEnum):
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class TerminalFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    code: str
    message: str
    details: dict[str, Any]

    @field_validator("code", "message")
    @classmethod
    def _validate_text(cls, value: str, info: Any) -> str:
        return _require_text(value, field=str(info.field_name))

    @field_validator("details")
    @classmethod
    def _validate_details(cls, value: object) -> dict[str, Any]:
        validated = validate_strict_json(value)
        if not isinstance(validated, dict):
            raise ValueError("details must be a JSON object")
        return validated


class LeaseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    semantic_key: str
    request_hash: str
    replay_policy: ReplayPolicy

    @field_validator("request_hash")
    @classmethod
    def _validate_request_hash(cls, value: str) -> str:
        return _require_request_hash(value)

    @model_validator(mode="after")
    def _validate(self) -> LeaseRequest:
        _require_text(self.semantic_key, field="semantic_key")
        return self


class Lease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    request: LeaseRequest
    owner_id: str
    attempt_id: str
    fence: StrictInt
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, value: datetime) -> datetime:
        return _require_utc(value, field="expires_at")

    @model_validator(mode="after")
    def _validate(self) -> Lease:
        _require_text(self.owner_id, field="owner_id", maximum=255)
        _require_text(self.attempt_id, field="attempt_id", maximum=255)
        if not 1 <= self.fence <= _MAX_FENCE:
            raise ValueError("fence must be a positive signed 64-bit integer")
        return self


class Terminal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    request: LeaseRequest
    outcome: TerminalOutcome
    owner_id: str
    attempt_id: str
    fence: StrictInt
    result_ref: ObjectReference | None = None
    failure: TerminalFailure | None = None

    @field_validator("result_ref", mode="before")
    @classmethod
    def _parse_result_ref(cls, value: object) -> ObjectReference | None:
        if value is None:
            return None
        if isinstance(value, ObjectReference):
            return value
        if isinstance(value, dict):
            return ObjectReference(
                schema=value["schema"],
                content_hash=value["content_hash"],
            )
        raise ValueError("result_ref must be an ObjectReference or mapping")

    @model_validator(mode="after")
    def _validate(self) -> Terminal:
        _require_text(self.owner_id, field="owner_id", maximum=255)
        _require_text(self.attempt_id, field="attempt_id", maximum=255)
        if not 1 <= self.fence <= _MAX_FENCE:
            raise ValueError("fence must be a positive signed 64-bit integer")
        if self.outcome is TerminalOutcome.SUCCEEDED:
            if self.result_ref is None or self.failure is not None:
                raise ValueError(
                    "a succeeded terminal requires only result_ref"
                )
        elif self.outcome is TerminalOutcome.FAILED:
            if self.result_ref is None or self.failure is None:
                raise ValueError(
                    "a failed terminal requires result_ref and failure"
                )
        elif self.failure is None or self.result_ref is not None:
            raise ValueError(
                "a recovery-required terminal requires only failure"
            )
        return self


class AcquireResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    request: LeaseRequest
    outcome: AcquireOutcome
    lease: Lease | None = None
    terminal: Terminal | None = None
    busy_expires_at: datetime | None = None
    existing_request_hash: str | None = None
    existing_replay_policy: ReplayPolicy | None = None

    @field_validator("busy_expires_at")
    @classmethod
    def _validate_busy_expires_at(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None:
            _require_utc(value, field="busy_expires_at")
        return value

    @field_validator("existing_request_hash")
    @classmethod
    def _validate_existing_request_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_request_hash(value)

    @model_validator(mode="after")
    def _validate(self) -> AcquireResult:
        populated = (
            self.lease is not None,
            self.terminal is not None,
            self.busy_expires_at is not None,
            self.existing_request_hash is not None,
            self.existing_replay_policy is not None,
        )
        if self.outcome is AcquireOutcome.ACQUIRED:
            if populated != (True, False, False, False, False):
                raise ValueError("ACQUIRED requires only a lease")
            if self.lease is not None and self.lease.request != self.request:
                raise ValueError("acquired lease must match the request")
        elif self.outcome is AcquireOutcome.BUSY:
            if populated != (False, False, True, False, False):
                raise ValueError("BUSY requires only busy_expires_at")
        elif self.outcome is AcquireOutcome.REQUEST_CONFLICT:
            if populated != (False, False, False, True, True):
                raise ValueError(
                    "REQUEST_CONFLICT requires the existing identity "
                    "and policy"
                )
        else:
            if populated != (False, True, False, False, False):
                raise ValueError(
                    "terminal acquisition outcomes require only terminal"
                )
            expected = {
                AcquireOutcome.SUCCEEDED: TerminalOutcome.SUCCEEDED,
                AcquireOutcome.FAILED: TerminalOutcome.FAILED,
                AcquireOutcome.RECOVERY_REQUIRED: (
                    TerminalOutcome.RECOVERY_REQUIRED
                ),
            }[self.outcome]
            if self.terminal is not None:
                if self.terminal.request != self.request:
                    raise ValueError("terminal must match the request")
                if self.terminal.outcome is not expected:
                    raise ValueError("terminal outcome does not match result")
        return self


class LeaseAuthorityError(RuntimeError):
    pass


class StaleLeaseError(LeaseAuthorityError):
    pass


class TerminalConflictError(LeaseAuthorityError):
    pass


class _AuthorityCorruptionError(LeaseAuthorityError):
    pass


@dataclass(frozen=True, slots=True)
class _LeaseRow:
    request: LeaseRequest
    state: _StoredState
    owner_id: str
    attempt_id: str
    fence: int
    expires_at: datetime | None
    terminal: Terminal | None

    @classmethod
    def leased(cls, lease: Lease) -> _LeaseRow:
        return cls(
            request=lease.request,
            state=_StoredState.LEASED,
            owner_id=lease.owner_id,
            attempt_id=lease.attempt_id,
            fence=lease.fence,
            expires_at=lease.expires_at,
            terminal=None,
        )

    @classmethod
    def terminalized(cls, terminal: Terminal) -> _LeaseRow:
        return cls(
            request=terminal.request,
            state=_StoredState(terminal.outcome.value),
            owner_id=terminal.owner_id,
            attempt_id=terminal.attempt_id,
            fence=terminal.fence,
            expires_at=None,
            terminal=terminal,
        )

    def lease(self) -> Lease:
        if self.state is not _StoredState.LEASED or self.expires_at is None:
            raise _AuthorityCorruptionError("row is not an active lease")
        return Lease(
            request=self.request,
            owner_id=self.owner_id,
            attempt_id=self.attempt_id,
            fence=self.fence,
            expires_at=self.expires_at,
        )


__all__ = [
    "AcquireOutcome",
    "AcquireResult",
    "Lease",
    "LeaseAuthorityError",
    "LeaseRequest",
    "ReplayPolicy",
    "StaleLeaseError",
    "Terminal",
    "TerminalConflictError",
    "TerminalFailure",
    "TerminalOutcome",
]
