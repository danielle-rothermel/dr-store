from __future__ import annotations

import threading
from datetime import timedelta
from threading import Event

import pytest

from dr_store.content_addressing import ObjectReference
from dr_store.lease import (
    LeaseAuthority,
    LeaseRequest,
    ReplayPolicy,
    StaleLeaseError,
)
from dr_store.testing import FakeClock

RESULT_REF = ObjectReference(schema="demo.record", content_hash="d" * 64)
LEASE_DURATION = timedelta(seconds=30)


class ManualRenewalWaitStrategy:
    def __init__(self) -> None:
        self._step = threading.Event()
        self.wait_completed = threading.Event()

    def wait(self, interval_seconds: float, stop: Event) -> bool:
        del interval_seconds
        if stop.is_set():
            return True
        self.wait_completed.clear()
        self._step.wait()
        self._step.clear()
        self.wait_completed.set()
        return stop.is_set()

    def wake(self) -> None:
        self._step.set()

    def release_once(self) -> None:
        self._step.set()

    def await_cycle(self, timeout: float = 1.0) -> None:
        assert self.wait_completed.wait(timeout), (
            "renewal wait strategy was not released"
        )


def _request() -> LeaseRequest:
    return LeaseRequest(
        semantic_key="maintenance.key",
        request_hash="e" * 64,
        replay_policy=ReplayPolicy.IDEMPOTENT,
    )


def test_maintenance_renewal_extends_lease() -> None:
    clock = FakeClock()
    strategy = ManualRenewalWaitStrategy()
    authority = LeaseAuthority.memory(
        clock=clock.now,
        _renewal_wait_strategy=strategy,
    )
    try:
        acquired = authority.acquire(
            _request(),
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=LEASE_DURATION,
        )
        assert acquired.lease is not None
        original_expiry = acquired.lease.expires_at
        maintenance = authority.maintain(
            acquired.lease,
            lease_duration=LEASE_DURATION,
        )
        with maintenance:
            clock.advance(timedelta(seconds=29))
            strategy.release_once()
            strategy.await_cycle()
            for _ in range(1000):
                if maintenance.lease.expires_at > original_expiry:
                    break
            else:
                pytest.fail("renewal did not extend lease")
            maintenance.succeed(result_ref=RESULT_REF)
    finally:
        authority.close()


def test_maintenance_terminalizes_through_handle() -> None:
    clock = FakeClock()
    strategy = ManualRenewalWaitStrategy()
    authority = LeaseAuthority.memory(
        clock=clock.now,
        _renewal_wait_strategy=strategy,
    )
    try:
        acquired = authority.acquire(
            _request(),
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=LEASE_DURATION,
        )
        assert acquired.lease is not None
        maintenance = authority.maintain(
            acquired.lease,
            lease_duration=LEASE_DURATION,
        )
        with maintenance:
            terminal = maintenance.succeed(result_ref=RESULT_REF)
        assert terminal.result_ref == RESULT_REF
        replay = authority.acquire(
            _request(),
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=LEASE_DURATION,
        )
        assert replay.terminal == terminal
    finally:
        authority.close()


def test_maintenance_clean_exit_requires_terminal() -> None:
    authority = LeaseAuthority.memory(
        clock=FakeClock().now,
        _renewal_wait_strategy=ManualRenewalWaitStrategy(),
    )
    try:
        acquired = authority.acquire(
            _request(),
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=LEASE_DURATION,
        )
        assert acquired.lease is not None
        maintenance = authority.maintain(
            acquired.lease,
            lease_duration=LEASE_DURATION,
        )
        with pytest.raises(RuntimeError, match="terminal publication"):
            with maintenance:
                pass
    finally:
        authority.close()


def test_maintenance_cannot_reenter() -> None:
    authority = LeaseAuthority.memory(
        clock=FakeClock().now,
        _renewal_wait_strategy=ManualRenewalWaitStrategy(),
    )
    try:
        acquired = authority.acquire(
            _request(),
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=LEASE_DURATION,
        )
        assert acquired.lease is not None
        maintenance = authority.maintain(
            acquired.lease,
            lease_duration=LEASE_DURATION,
        )
        with maintenance:
            with pytest.raises(RuntimeError, match="re-entered"):
                maintenance.__enter__()
            maintenance.succeed(result_ref=RESULT_REF)
    finally:
        authority.close()


def test_maintenance_surfaces_renewal_loss() -> None:
    clock = FakeClock()
    strategy = ManualRenewalWaitStrategy()
    authority = LeaseAuthority.memory(
        clock=clock.now,
        _renewal_wait_strategy=strategy,
    )
    try:
        acquired = authority.acquire(
            _request(),
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=LEASE_DURATION,
        )
        assert acquired.lease is not None
        maintenance = authority.maintain(
            acquired.lease,
            lease_duration=LEASE_DURATION,
        )
        maintenance.__enter__()
        try:
            clock.advance(LEASE_DURATION + timedelta(seconds=1))
            strategy.release_once()
            strategy.await_cycle()
            for _ in range(1000):
                try:
                    maintenance.check()
                except StaleLeaseError:
                    break
            else:
                pytest.fail("renewal loss was not recorded")
            with pytest.raises(StaleLeaseError):
                maintenance.check()
            with pytest.raises(StaleLeaseError):
                maintenance.succeed(result_ref=RESULT_REF)
        finally:
            with pytest.raises(StaleLeaseError):
                maintenance.__exit__(None, None, None)
    finally:
        authority.close()


def test_maintenance_terminalize_after_exit_raises() -> None:
    authority = LeaseAuthority.memory(
        clock=FakeClock().now,
        _renewal_wait_strategy=ManualRenewalWaitStrategy(),
    )
    try:
        acquired = authority.acquire(
            _request(),
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=LEASE_DURATION,
        )
        assert acquired.lease is not None
        maintenance = authority.maintain(
            acquired.lease,
            lease_duration=LEASE_DURATION,
        )
        entered = maintenance.__enter__()
        maintenance.succeed(result_ref=RESULT_REF)
        maintenance.__exit__(None, None, None)
        with pytest.raises(RuntimeError, match="already terminalized"):
            entered.succeed(result_ref=RESULT_REF)
    finally:
        authority.close()
