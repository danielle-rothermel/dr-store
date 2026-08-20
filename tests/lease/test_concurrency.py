from __future__ import annotations

import concurrent.futures
import threading

from dr_store.lease import AcquireOutcome, LeaseAuthority
from tests.lease.conftest import LEASE_DURATION, make_request


class _CountingObserver:
    def __init__(self) -> None:
        self.attempted = 0
        self.acquired = 0
        self._lock = threading.Lock()

    def transaction_attempted(self) -> None:
        with self._lock:
            self.attempted += 1

    def transaction_acquired(self) -> None:
        with self._lock:
            self.acquired += 1


def test_concurrent_acquire_serializes_on_sqlite(tmp_path) -> None:
    path = tmp_path / "lease.sqlite3"
    observer = _CountingObserver()
    authority = LeaseAuthority.sqlite(
        path,
        _transaction_observer=observer,
    )
    request = make_request(semantic_key="concurrency.key")
    try:

        def acquire(owner_id: str):
            return authority.acquire(
                request,
                owner_id=owner_id,
                attempt_id=f"attempt-{owner_id}",
                lease_duration=LEASE_DURATION,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(acquire, [f"owner-{index}" for index in range(8)])
            )
    finally:
        authority.close()

    acquired = [
        result
        for result in results
        if result.outcome is AcquireOutcome.ACQUIRED
    ]
    busy = [
        result for result in results if result.outcome is AcquireOutcome.BUSY
    ]
    assert len(acquired) == 1
    assert len(busy) == 7
    assert observer.attempted >= 8
    assert observer.acquired >= 8
