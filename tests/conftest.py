"""Shared fixtures: run the whole contract against every backend.

The contract is backend-neutral, so the same behavioral tests run against
the in-memory backend and the durable SQLite backend via one parametrized
``store`` fixture. A behavior proven for one backend but not the other is
not proven for the contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dr_store import MemoryBackend, ObjectStore, SqliteBackend

if TYPE_CHECKING:
    from pathlib import Path

    from dr_store.backends.base import Backend


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Backend:
    if request.param == "memory":
        return MemoryBackend()
    return SqliteBackend(tmp_path / "store.db")


@pytest.fixture
def store(backend: Backend) -> ObjectStore:
    return ObjectStore(backend)
