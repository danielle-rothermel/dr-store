from __future__ import annotations

import os
from datetime import timedelta

import pytest

from dr_store.lease import (
    AcquireOutcome,
    LeaseAuthority,
    LeaseRequest,
    ReplayPolicy,
)
from dr_store.localfs import PrivateDirectory
from dr_store.sync import BlockingObjectStore
from dr_store.testing import (
    FakeClock,
    temp_lease_authority,
    temp_private_directory,
    temp_sqlite_lease_authority,
    temp_sqlite_store,
)


def test_fake_clock_advances_memory_lease_expiry() -> None:
    with temp_lease_authority() as (authority, clock):
        request = LeaseRequest(
            semantic_key="testing.key",
            request_hash="1" * 64,
            replay_policy=ReplayPolicy.NO_REDRIVE,
        )
        acquired = authority.acquire(
            request,
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=timedelta(seconds=30),
        )
        assert acquired.outcome is AcquireOutcome.ACQUIRED
        clock.advance(timedelta(minutes=5))
        recovery = authority.acquire(
            request,
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=timedelta(seconds=30),
        )
        assert recovery.outcome is AcquireOutcome.RECOVERY_REQUIRED


def test_temp_sqlite_store_round_trip() -> None:
    with temp_sqlite_store() as store:
        assert isinstance(store, BlockingObjectStore)
        reference, _status = store.put("demo.record", {"value": 1})
        assert store.get(reference) == {"value": 1}


def test_temp_sqlite_lease_authority_acquire_round_trip() -> None:
    from datetime import timedelta

    from dr_store.lease import AcquireOutcome

    with temp_sqlite_lease_authority() as authority:
        assert isinstance(authority, LeaseAuthority)
        request = LeaseRequest(
            semantic_key="testing.lease/key",
            request_hash="2" * 64,
            replay_policy=ReplayPolicy.IDEMPOTENT,
        )
        acquired = authority.acquire(
            request,
            owner_id="owner",
            attempt_id="attempt",
            lease_duration=timedelta(seconds=30),
        )
        assert acquired.outcome is AcquireOutcome.ACQUIRED
        assert acquired.lease is not None


def test_temp_lease_authority_exposes_fake_clock() -> None:
    with temp_lease_authority() as (_authority, clock):
        assert isinstance(clock, FakeClock)


def test_temp_sqlite_store_cleans_up_on_error() -> None:
    with pytest.raises(RuntimeError), temp_sqlite_store():
        raise RuntimeError("cleanup probe")


def test_temp_private_directory_is_usable_and_cleaned_up() -> None:
    with temp_private_directory() as directory:
        assert isinstance(directory, PrivateDirectory)
        kept = directory.path
        fd = directory.open_regular("child", os.O_WRONLY | os.O_CREAT)
        os.close(fd)
        assert "child" in directory.list_names()
    assert not kept.exists()


def test_temp_private_directory_cleans_up_on_error() -> None:
    with pytest.raises(RuntimeError), temp_private_directory():
        raise RuntimeError("cleanup probe")
