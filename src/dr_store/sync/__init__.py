"""Blocking sync facade over async ``ObjectStore``.

``open_sqlite`` and ``persistent_sqlite`` own a dedicated event-loop thread and
run backend operations through ``run_coroutine_threadsafe``. Close waits for
in-flight operations to settle; callers needing prompt teardown should quiesce
first. Post-close use raises ``SyncSessionClosedError``. This is distinct from
PostgreSQL **enlisted** methods, which join a caller-owned SQLAlchemy
transaction for evidence checkpoint integration.
"""

from __future__ import annotations

from dr_store.sync.blocking import (
    BlockingObjectStore,
    SyncSessionClosedError,
    close_all_persistent,
    close_persistent,
    open_sqlite,
    persistent_sqlite,
)

__all__ = [
    "BlockingObjectStore",
    "SyncSessionClosedError",
    "close_all_persistent",
    "close_persistent",
    "open_sqlite",
    "persistent_sqlite",
]
