from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Protocol

from dr_store.lease._memory import _MemoryStore
from dr_store.lease._postgres import _Connect, _PostgreSQLStore
from dr_store.lease._sqlite import _SQLiteStore
from dr_store.lease._storage import _Store, _timestamp_text
from dr_store.lease.models import (
    AcquireOutcome,
    AcquireResult,
    Lease,
    LeaseAuthorityError,
    LeaseRequest,
    ReplayPolicy,
    StaleLeaseError,
    Terminal,
    TerminalConflictError,
    TerminalFailure,
    TerminalOutcome,
    _AuthorityCorruptionError,
    _LeaseRow,
    _require_lease_duration,
    _require_text,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from dr_store.content_addressing import ObjectReference
    from dr_store.relational import TransactionObserver

_MAX_FENCE = (1 << 63) - 1
_RECOVERY_MESSAGE = (
    "the non-redrivable effect lease expired without a terminal outcome"
)


class _RenewalWaitStrategy(Protocol):
    def wait(self, interval_seconds: float, stop: Event) -> bool: ...

    def wake(self) -> None: ...


class _EventRenewalWaitStrategy:
    def wait(self, interval_seconds: float, stop: Event) -> bool:
        return stop.wait(interval_seconds)

    def wake(self) -> None:
        pass


_EVENT_RENEWAL_WAIT = _EventRenewalWaitStrategy()


def _recovery_failure(row: _LeaseRow) -> TerminalFailure:
    if row.expires_at is None:
        raise _AuthorityCorruptionError("expired lease has no expiration")
    payload = {
        "semantic_key": row.request.semantic_key,
        "request_hash": row.request.request_hash,
        "owner_id": row.owner_id,
        "attempt_id": row.attempt_id,
        "fence": row.fence,
        "expires_at": _timestamp_text(row.expires_at),
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
    return TerminalFailure(
        code=f"effect-recovery:{digest}",
        message=_RECOVERY_MESSAGE,
        details=payload,
    )


def _acquire_terminal(
    request: LeaseRequest, terminal: Terminal
) -> AcquireResult:
    return AcquireResult(
        request=request,
        outcome=AcquireOutcome(terminal.outcome.value),
        terminal=terminal,
    )


class LeaseMaintenance:
    def __init__(
        self,
        authority: LeaseAuthority,
        lease: Lease,
        lease_duration: timedelta,
        renewal_wait_strategy: _RenewalWaitStrategy,
    ) -> None:
        self._authority = authority
        self._lease = lease
        self._lease_duration = authority._validate_lease_duration(
            lease_duration
        )
        self._interval_seconds = self._lease_duration.total_seconds() / 3
        self._renewal_wait_strategy = renewal_wait_strategy
        self._stop = Event()
        self._lock = Lock()
        self._loss: BaseException | None = None
        self._thread = Thread(
            target=self._run,
            name=f"effect-lease-{lease.attempt_id}",
            daemon=True,
        )
        self._entered = False
        self._exited = False
        self._terminalizing = False
        self._terminalization_started = False
        self._terminalized = False

    @property
    def lease(self) -> Lease:
        self.check()
        with self._lock:
            return self._lease

    def check(self) -> None:
        with self._lock:
            loss = self._loss
        if loss is None:
            return
        if isinstance(loss, LeaseAuthorityError):
            raise loss
        raise LeaseAuthorityError(
            f"lease maintenance failed: {type(loss).__name__}: {loss}"
        ) from loss

    def _stop_renewer(self) -> None:
        self._stop.set()
        self._renewal_wait_strategy.wake()

    def _restart_renewer(self) -> None:
        self._thread = Thread(
            target=self._run,
            name=f"effect-lease-{self._lease.attempt_id}",
            daemon=True,
        )
        self._thread.start()

    def _abort_terminalization(self) -> None:
        with self._lock:
            if self._terminalization_started:
                return
            was_terminalizing = self._terminalizing
            self._terminalizing = False
            if not was_terminalizing or self._loss is not None:
                return
            self._stop.clear()
        self._restart_renewer()

    def _commit_terminalization(self) -> None:
        with self._lock:
            self._terminalization_started = True
            self._terminalized = True
            self._terminalizing = False

    def _prepare_for_terminalization(self) -> Lease:
        with self._lock:
            if self._terminalization_started:
                raise RuntimeError("lease maintenance is already terminalized")
            if self._terminalizing:
                raise RuntimeError("terminalization already in progress")
            loss = self._loss
            if loss is None and (
                not self._entered or self._exited or self._stop.is_set()
            ):
                raise RuntimeError(
                    "terminal publication requires an active maintenance "
                    "context"
                )
            if loss is None:
                self._terminalizing = True
                self._stop_renewer()

        try:
            if loss is not None:
                self.check()
                raise AssertionError("lease loss check returned unexpectedly")

            self._thread.join()
            self.check()
            with self._lock:
                return self._lease
        except BaseException:
            self._abort_terminalization()
            raise

    def succeed(self, *, result_ref: ObjectReference) -> Terminal:
        lease = self._prepare_for_terminalization()
        try:
            terminal = self._authority.succeed(lease, result_ref=result_ref)
        except BaseException:
            self._abort_terminalization()
            raise
        self._commit_terminalization()
        return terminal

    def fail(
        self,
        *,
        result_ref: ObjectReference,
        failure: TerminalFailure,
    ) -> Terminal:
        lease = self._prepare_for_terminalization()
        try:
            terminal = self._authority.fail(
                lease,
                result_ref=result_ref,
                failure=failure,
            )
        except BaseException:
            self._abort_terminalization()
            raise
        self._commit_terminalization()
        return terminal

    def _run(self) -> None:
        while not self._renewal_wait_strategy.wait(
            self._interval_seconds, self._stop
        ):
            with self._lock:
                current = self._lease
            try:
                renewed = self._authority.renew(
                    current,
                    lease_duration=self._lease_duration,
                )
            except BaseException as exc:  # noqa: BLE001 - record any renewal loss
                with self._lock:
                    self._loss = exc
                self._stop_renewer()
                return
            with self._lock:
                self._lease = renewed

    def __enter__(self) -> LeaseMaintenance:
        with self._lock:
            if self._entered:
                raise RuntimeError("lease maintenance cannot be re-entered")
            self._entered = True
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        with self._lock:
            self._exited = True
            self._stop_renewer()
        self._thread.join()
        with self._lock:
            terminalized = self._terminalized
        if not args or args[0] is None:
            self.check()
            if not terminalized:
                raise RuntimeError(
                    "clean lease maintenance exit requires terminal "
                    "publication"
                )


class LeaseAuthority:
    def __init__(
        self,
        store: _Store,
        *,
        _renewal_wait_strategy: _RenewalWaitStrategy | None = None,
    ) -> None:
        self._store = store
        self._renewal_wait_strategy = (
            _EVENT_RENEWAL_WAIT
            if _renewal_wait_strategy is None
            else _renewal_wait_strategy
        )
        self._store.initialize()

    @classmethod
    def memory(
        cls,
        *,
        clock: Callable[[], datetime] | None = None,
        _renewal_wait_strategy: _RenewalWaitStrategy | None = None,
    ) -> LeaseAuthority:
        authority_clock = (
            (lambda: datetime.now(UTC)) if clock is None else clock
        )
        return cls(
            _MemoryStore(authority_clock),
            _renewal_wait_strategy=_renewal_wait_strategy,
        )

    @classmethod
    def sqlite(
        cls,
        path: str | Path,
        *,
        _transaction_observer: TransactionObserver | None = None,
    ) -> LeaseAuthority:
        return cls(
            _SQLiteStore(
                path,
                transaction_observer=_transaction_observer,
            )
        )

    @classmethod
    def postgresql(
        cls,
        dsn: str,
        *,
        _connect: _Connect | None = None,
    ) -> LeaseAuthority:
        return cls(_PostgreSQLStore(dsn, connect=_connect))

    def acquire(
        self,
        request: LeaseRequest,
        *,
        owner_id: str,
        attempt_id: str,
        lease_duration: timedelta,
    ) -> AcquireResult:
        _require_text(owner_id, field="owner_id", maximum=255)
        _require_text(attempt_id, field="attempt_id", maximum=255)
        validated_duration = self._validate_lease_duration(lease_duration)

        def transition(
            row: _LeaseRow | None, now: datetime
        ) -> tuple[_LeaseRow, AcquireResult]:
            lease_expires_at = now + validated_duration
            if row is None:
                lease = Lease(
                    request=request,
                    owner_id=owner_id,
                    attempt_id=attempt_id,
                    fence=1,
                    expires_at=lease_expires_at,
                )
                return (
                    _LeaseRow.leased(lease),
                    AcquireResult(
                        request=request,
                        outcome=AcquireOutcome.ACQUIRED,
                        lease=lease,
                    ),
                )
            if row.request != request:
                return (
                    row,
                    AcquireResult(
                        request=request,
                        outcome=AcquireOutcome.REQUEST_CONFLICT,
                        existing_request_hash=(row.request.request_hash),
                        existing_replay_policy=row.request.replay_policy,
                    ),
                )
            if row.terminal is not None:
                return row, _acquire_terminal(request, row.terminal)
            lease = row.lease()
            if lease.expires_at > now:
                if (
                    lease.owner_id == owner_id
                    and lease.attempt_id == attempt_id
                ):
                    return (
                        row,
                        AcquireResult(
                            request=request,
                            outcome=AcquireOutcome.ACQUIRED,
                            lease=lease,
                        ),
                    )
                return (
                    row,
                    AcquireResult(
                        request=request,
                        outcome=AcquireOutcome.BUSY,
                        busy_expires_at=lease.expires_at,
                    ),
                )
            if request.replay_policy is ReplayPolicy.NO_REDRIVE:
                terminal = Terminal(
                    request=request,
                    outcome=TerminalOutcome.RECOVERY_REQUIRED,
                    owner_id=lease.owner_id,
                    attempt_id=lease.attempt_id,
                    fence=lease.fence,
                    failure=_recovery_failure(row),
                )
                return (
                    _LeaseRow.terminalized(terminal),
                    _acquire_terminal(request, terminal),
                )
            if lease.fence == _MAX_FENCE:
                raise LeaseAuthorityError(
                    "effect fence exhausted the signed 64-bit range"
                )
            takeover = Lease(
                request=request,
                owner_id=owner_id,
                attempt_id=attempt_id,
                fence=lease.fence + 1,
                expires_at=lease_expires_at,
            )
            return (
                _LeaseRow.leased(takeover),
                AcquireResult(
                    request=request,
                    outcome=AcquireOutcome.ACQUIRED,
                    lease=takeover,
                ),
            )

        return self._store.transaction(request.semantic_key, transition)

    def renew(
        self,
        lease: Lease,
        *,
        lease_duration: timedelta,
    ) -> Lease:
        validated_duration = self._validate_lease_duration(lease_duration)

        def transition(
            row: _LeaseRow | None, now: datetime
        ) -> tuple[_LeaseRow | None, Lease]:
            if row is None or row.request != lease.request:
                raise StaleLeaseError("effect lease no longer exists")
            current = row.lease() if row.terminal is None else None
            if (
                current is None
                or current.owner_id != lease.owner_id
                or current.attempt_id != lease.attempt_id
                or current.fence != lease.fence
                or current.expires_at != lease.expires_at
                or current.expires_at <= now
            ):
                raise StaleLeaseError("effect lease is stale")
            lease_expires_at = now + validated_duration
            if lease_expires_at <= current.expires_at:
                return row, current
            renewed = Lease(
                request=current.request,
                owner_id=current.owner_id,
                attempt_id=current.attempt_id,
                fence=current.fence,
                expires_at=lease_expires_at,
            )
            return _LeaseRow.leased(renewed), renewed

        return self._store.transaction(lease.request.semantic_key, transition)

    def succeed(
        self,
        lease: Lease,
        *,
        result_ref: ObjectReference,
    ) -> Terminal:
        terminal = Terminal(
            request=lease.request,
            outcome=TerminalOutcome.SUCCEEDED,
            owner_id=lease.owner_id,
            attempt_id=lease.attempt_id,
            fence=lease.fence,
            result_ref=result_ref,
        )
        return self._terminalize(lease, terminal=terminal)

    def fail(
        self,
        lease: Lease,
        *,
        result_ref: ObjectReference,
        failure: TerminalFailure,
    ) -> Terminal:
        terminal = Terminal(
            request=lease.request,
            outcome=TerminalOutcome.FAILED,
            owner_id=lease.owner_id,
            attempt_id=lease.attempt_id,
            fence=lease.fence,
            result_ref=result_ref,
            failure=failure,
        )
        return self._terminalize(lease, terminal=terminal)

    def verify_terminal(self, terminal: Terminal) -> Terminal:
        validated = Terminal.model_validate_json(terminal.model_dump_json())

        def transition(
            row: _LeaseRow | None, now: datetime
        ) -> tuple[_LeaseRow | None, Terminal]:
            del now
            authoritative = None if row is None else row.terminal
            if authoritative is None or authoritative != validated:
                raise TerminalConflictError(
                    "the supplied effect terminal is not authoritative"
                )
            return row, authoritative

        return self._store.transaction(
            validated.request.semantic_key, transition
        )

    def _terminalize(
        self,
        lease: Lease,
        *,
        terminal: Terminal,
    ) -> Terminal:
        def transition(
            row: _LeaseRow | None, now: datetime
        ) -> tuple[_LeaseRow | None, Terminal]:
            if row is None or row.request != lease.request:
                raise StaleLeaseError("effect lease no longer exists")
            if row.terminal is not None:
                if row.terminal == terminal:
                    return row, row.terminal
                raise TerminalConflictError(
                    "a different terminal outcome is already authoritative"
                )
            current = row.lease()
            if (
                current.owner_id != lease.owner_id
                or current.attempt_id != lease.attempt_id
                or current.fence != lease.fence
                or current.expires_at != lease.expires_at
                or current.expires_at <= now
            ):
                raise StaleLeaseError("effect lease is stale")
            return _LeaseRow.terminalized(terminal), terminal

        return self._store.transaction(lease.request.semantic_key, transition)

    def maintain(
        self,
        lease: Lease,
        *,
        lease_duration: timedelta,
    ) -> LeaseMaintenance:
        return LeaseMaintenance(
            self,
            lease,
            lease_duration,
            self._renewal_wait_strategy,
        )

    def _validate_lease_duration(self, value: timedelta) -> timedelta:
        return self._store.validate_lease_duration(
            _require_lease_duration(value)
        )

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> LeaseAuthority:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = [
    "AcquireOutcome",
    "AcquireResult",
    "Lease",
    "LeaseAuthority",
    "LeaseAuthorityError",
    "LeaseMaintenance",
    "LeaseRequest",
    "ReplayPolicy",
    "StaleLeaseError",
    "Terminal",
    "TerminalConflictError",
    "TerminalFailure",
    "TerminalOutcome",
]
