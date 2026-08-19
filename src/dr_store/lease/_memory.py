from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING

from dr_store.lease.models import _LeaseRow, _require_utc

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, timedelta

    from dr_store.lease._storage import _T, _Transition


class _MemoryStore:
    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._rows: dict[str, _LeaseRow] = {}
        self._lock = RLock()
        self._clock = clock

    def initialize(self) -> None:
        pass

    def validate_lease_duration(self, duration: timedelta) -> timedelta:
        return duration

    def transaction(
        self,
        semantic_key: str,
        transition: _Transition[_T],
    ) -> _T:
        with self._lock:
            now = _require_utc(self._clock(), field="authority clock")
            updated, result = transition(self._rows.get(semantic_key), now)
            if updated is None:
                self._rows.pop(semantic_key, None)
            else:
                self._rows[semantic_key] = updated
            return result

    def close(self) -> None:
        pass
