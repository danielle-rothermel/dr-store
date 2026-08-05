"""Backend implementations for the Object Store.

All backends satisfy the :class:`Backend` protocol and are interchangeable
behind :class:`~dr_store.object_store.ObjectStore`. Contract semantics live in
the store; backends only guarantee atomic, durable compare-and-set primitives.
"""

from __future__ import annotations

from dr_store.storage_backends.contract import Backend, BindOutcome, PutOutcome
from dr_store.storage_backends.memory import MemoryBackend
from dr_store.storage_backends.sqlite import SqliteBackend

__all__ = [
    "Backend",
    "BindOutcome",
    "MemoryBackend",
    "PutOutcome",
    "SqliteBackend",
]
