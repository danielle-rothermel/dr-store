from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_DEDICATED_DATABASE = "dr_store_test"


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn.removeprefix("postgresql://")
    return dsn


@pytest.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
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
