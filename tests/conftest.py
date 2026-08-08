from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from dr_store import Backend, MemoryBackend, ObjectStore, SqliteBackend

type BackendFactory = Callable[[Path], Awaitable[Backend]]


async def _memory_backend(_path: Path) -> Backend:
    return MemoryBackend()


async def _sqlite_backend(path: Path) -> Backend:
    return await SqliteBackend.open(path)


@pytest.fixture(
    params=[
        pytest.param(_memory_backend, id="memory"),
        pytest.param(_sqlite_backend, id="sqlite"),
    ]
)
def backend_factory(request: pytest.FixtureRequest) -> BackendFactory:
    return request.param


@pytest.fixture
async def backend(
    backend_factory: BackendFactory, tmp_path: Path
) -> AsyncIterator[Backend]:
    instance = await backend_factory(tmp_path / "store.db")
    yield instance
    if isinstance(instance, SqliteBackend):
        await instance.aclose()


@pytest.fixture
def store(backend: Backend) -> ObjectStore:
    return ObjectStore(backend)
