from __future__ import annotations

import os
from typing import TYPE_CHECKING

import asyncpg
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_DEDICATED_DATABASE = "dr_store_test"


@pytest.fixture
async def postgres_pool() -> AsyncIterator[asyncpg.Pool]:
    dsn = os.environ.get("DR_STORE_POSTGRES_DSN")
    if dsn is None:
        if os.environ.get("DR_STORE_REQUIRE_POSTGRES") == "1":
            pytest.fail(
                "DR_STORE_REQUIRE_POSTGRES=1 requires DR_STORE_POSTGRES_DSN"
            )
        pytest.skip("DR_STORE_POSTGRES_DSN is not configured")

    pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=4,
        server_settings={"search_path": "pg_catalog"},
    )
    dedicated_database_verified = False
    try:
        async with pool.acquire() as connection:
            database = await connection.fetchval(
                "SELECT pg_catalog.current_database()"
            )
            if database != _DEDICATED_DATABASE:
                pytest.fail(
                    "PostgreSQL integration schema reset is allowed only in "
                    f"the {_DEDICATED_DATABASE!r} database; connected to "
                    f"{database!r}"
                )
            dedicated_database_verified = True
            await connection.execute("DROP SCHEMA IF EXISTS dr_store CASCADE")

        yield pool
    finally:
        if not pool.is_closing():
            if dedicated_database_verified:
                async with pool.acquire() as connection:
                    database = await connection.fetchval(
                        "SELECT pg_catalog.current_database()"
                    )
                    if database != _DEDICATED_DATABASE:
                        pytest.fail(
                            "Refusing PostgreSQL integration cleanup outside "
                            f"{_DEDICATED_DATABASE!r}"
                        )
                    await connection.execute(
                        "DROP SCHEMA IF EXISTS dr_store CASCADE"
                    )
            await pool.close()
