"""Backend implementations for the Object Store.

All backends satisfy the :class:`Backend` protocol and are interchangeable
behind :class:`~dr_store.store.ObjectStore`. Contract semantics live in the
store; backends only guarantee atomic, durable compare-and-set primitives.
"""

from __future__ import annotations

from dr_store.backends.base import Backend
from dr_store.backends.memory import MemoryBackend
from dr_store.backends.sqlite import SqliteBackend
from dr_store.backends.types import BindOutcome, PutOutcome

__all__ = [
    "Backend",
    "BindOutcome",
    "MemoryBackend",
    "PutOutcome",
    "SqliteBackend",
]
