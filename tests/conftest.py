from __future__ import annotations

import os
from collections.abc import AsyncIterator  # noqa: TC003
from contextlib import asynccontextmanager
from pathlib import Path  # noqa: TC003

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from dr_store import (
    Backend,
    MemoryBackend,
    ObjectStore,
    PostgresBackend,
    SqliteBackend,
    install_postgres,
)

_DEDICATED_DATABASE = "dr_store_test"


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn.removeprefix("postgresql://")
    return dsn


def _sync_dsn(dsn: str) -> str:
    return _async_dsn(dsn)


def _backend_params() -> list[object]:
    params: list[object] = [
        pytest.param("memory", id="memory"),
        pytest.param("sqlite", id="sqlite"),
    ]
    if os.environ.get("DR_STORE_POSTGRES_DSN") is not None:
        params.append(pytest.param("postgres", id="postgres"))
    return params


@asynccontextmanager
async def _postgres_engine() -> AsyncIterator[AsyncEngine]:
    dsn = os.environ.get("DR_STORE_POSTGRES_DSN")
    if dsn is None:
        if os.environ.get("DR_STORE_REQUIRE_POSTGRES") == "1":
            pytest.fail(
                "DR_STORE_REQUIRE_POSTGRES=1 requires DR_STORE_POSTGRES_DSN"
            )
        pytest.skip("DR_STORE_POSTGRES_DSN is not configured")

    engine = create_async_engine(
        _async_dsn(dsn),
        pool_size=4,
        max_overflow=0,
        connect_args={"options": "-c search_path=pg_catalog"},
    )
    dedicated_database_verified = False
    try:
        async with engine.connect() as connection:
            database = await connection.scalar(
                text("SELECT pg_catalog.current_database()")
            )
            if database != _DEDICATED_DATABASE:
                pytest.fail(
                    "PostgreSQL integration schema reset is allowed only in "
                    f"the {_DEDICATED_DATABASE!r} database; connected to "
                    f"{database!r}"
                )
            dedicated_database_verified = True
            await connection.execute(
                text("DROP SCHEMA IF EXISTS dr_store CASCADE")
            )
            await connection.commit()

        yield engine
    finally:
        if dedicated_database_verified:
            async with engine.connect() as connection:
                database = await connection.scalar(
                    text("SELECT pg_catalog.current_database()")
                )
                if database != _DEDICATED_DATABASE:
                    pytest.fail(
                        "Refusing PostgreSQL integration cleanup outside "
                        f"{_DEDICATED_DATABASE!r}"
                    )
                await connection.execute(
                    text("DROP SCHEMA IF EXISTS dr_store CASCADE")
                )
                await connection.commit()
        await engine.dispose()


@pytest.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    async with _postgres_engine() as engine:
        yield engine


@pytest.fixture
async def postgres_backend(postgres_engine: AsyncEngine) -> PostgresBackend:
    await install_postgres(postgres_engine)
    return await PostgresBackend.open(postgres_engine)


@pytest.fixture(params=_backend_params())
async def backend(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[Backend]:
    kind = request.param
    if kind == "memory":
        yield MemoryBackend()
    elif kind == "sqlite":
        instance = await SqliteBackend.open(tmp_path / "store.db")
        yield instance
        await instance.aclose()
    else:
        async with _postgres_engine() as engine:
            await install_postgres(engine)
            yield await PostgresBackend.open(engine)


@pytest.fixture
def store(backend: Backend) -> ObjectStore:
    return ObjectStore(backend)
