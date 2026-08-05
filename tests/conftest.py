"""Named factories for comparative Memory and SQLite contract tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dr_store import Backend, MemoryBackend, ObjectStore, SqliteBackend

type BackendFactory = Callable[[Path], Backend]


def _memory_backend(_path: Path) -> Backend:
    return MemoryBackend()


def _sqlite_backend(path: Path) -> Backend:
    return SqliteBackend(path)


@pytest.fixture(
    params=[
        pytest.param(_memory_backend, id="memory"),
        pytest.param(_sqlite_backend, id="sqlite"),
    ]
)
def backend_factory(request: pytest.FixtureRequest) -> BackendFactory:
    return request.param


@pytest.fixture
def backend(backend_factory: BackendFactory, tmp_path: Path) -> Backend:
    return backend_factory(tmp_path / "store.db")


@pytest.fixture
def store(backend: Backend) -> ObjectStore:
    return ObjectStore(backend)
