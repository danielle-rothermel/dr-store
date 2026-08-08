from __future__ import annotations

import asyncio
import inspect
from typing import cast

import asyncpg
import pytest

from dr_store import install_postgres
from dr_store.storage_backends import postgresql


@pytest.mark.parametrize("version_num", [150_000, 190_000])
def test_rejects_unsupported_postgresql_versions(version_num: int) -> None:
    with pytest.raises(
        RuntimeError, match="PostgreSQL 16 through 18 is required"
    ):
        postgresql._validate_database(
            version_num=version_num, server_encoding="UTF8"
        )


def test_rejects_non_utf8_server_encoding() -> None:
    with pytest.raises(RuntimeError, match="encoding must be UTF-8"):
        postgresql._validate_database(
            version_num=160_000, server_encoding="SQL_ASCII"
        )


async def test_rejects_non_pool_without_connection_work() -> None:
    with pytest.raises(TypeError, match=r"pool must be an asyncpg\.Pool"):
        await install_postgres(cast("asyncpg.Pool", object()))


def test_installer_has_no_credential_bearing_api_or_state() -> None:
    signature = inspect.signature(install_postgres)
    assert list(signature.parameters) == ["pool"]
    assert install_postgres.__closure__ is None
    assert install_postgres.__dict__ == {}
    assert "dsn" not in inspect.getsource(postgresql).casefold()


async def test_installs_absent_fixed_schema_transactionally(
    postgres_pool: asyncpg.Pool,
) -> None:
    await install_postgres(postgres_pool)

    async with postgres_pool.acquire() as connection:
        assert await connection.fetchval("SHOW search_path") == "pg_catalog"
        assert await connection.fetchval(
            """
            SELECT pg_catalog.array_agg(
                tables.table_name ORDER BY tables.table_name
            )
            FROM information_schema.tables
            WHERE tables.table_schema = 'dr_store'
            """
        ) == ["bindings", "objects"]


async def test_installation_rolls_back_every_object_on_ddl_failure(
    postgres_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        postgresql,
        "_INSTALL_SQL",
        "CREATE SCHEMA dr_store; SELECT 1 / 0;",
    )

    with pytest.raises(asyncpg.DivisionByZeroError):
        await install_postgres(postgres_pool)

    async with postgres_pool.acquire() as connection:
        assert not await connection.fetchval(
            """
            SELECT EXISTS(
                SELECT 1
                FROM pg_catalog.pg_namespace
                WHERE pg_namespace.nspname = 'dr_store'
            )
            """
        )


async def test_repeated_installation_fails_without_adoption(
    postgres_pool: asyncpg.Pool,
) -> None:
    await install_postgres(postgres_pool)

    with pytest.raises(asyncpg.DuplicateSchemaError):
        await install_postgres(postgres_pool)


async def test_preexisting_namespace_is_not_adopted(
    postgres_pool: asyncpg.Pool,
) -> None:
    async with postgres_pool.acquire() as connection:
        await connection.execute("CREATE SCHEMA dr_store")

    with pytest.raises(asyncpg.DuplicateSchemaError):
        await install_postgres(postgres_pool)

    async with postgres_pool.acquire() as connection:
        assert (
            await connection.fetchval(
                """
            SELECT pg_catalog.count(*)
            FROM information_schema.tables
            WHERE tables.table_schema = 'dr_store'
            """
            )
            == 0
        )


async def test_concurrent_installation_has_one_success_and_one_failure(
    postgres_pool: asyncpg.Pool,
) -> None:
    results = await asyncio.gather(
        install_postgres(postgres_pool),
        install_postgres(postgres_pool),
        return_exceptions=True,
    )

    assert sum(result is None for result in results) == 1
    failures = [
        result for result in results if isinstance(result, BaseException)
    ]
    assert len(failures) == 1
    assert isinstance(failures[0], asyncpg.PostgresError)


async def test_catalog_pins_qualified_exact_text_and_hash_leading_keys(
    postgres_pool: asyncpg.Pool,
) -> None:
    await install_postgres(postgres_pool)

    async with postgres_pool.acquire() as connection:
        columns = await connection.fetch(
            """
            SELECT
                table_record.relname,
                column_record.attname,
                pg_catalog.format_type(
                    column_record.atttypid, column_record.atttypmod
                ) AS type_name,
                collation_schema.nspname AS collation_schema,
                collation_record.collname AS collation_name
            FROM pg_catalog.pg_attribute AS column_record
            JOIN pg_catalog.pg_class AS table_record
                ON table_record.oid = column_record.attrelid
            JOIN pg_catalog.pg_namespace AS table_schema
                ON table_schema.oid = table_record.relnamespace
            JOIN pg_catalog.pg_collation AS collation_record
                ON collation_record.oid = column_record.attcollation
            JOIN pg_catalog.pg_namespace AS collation_schema
                ON collation_schema.oid = collation_record.collnamespace
            WHERE table_schema.nspname = 'dr_store'
                AND table_record.relname IN ('objects', 'bindings')
                AND column_record.attnum > 0
                AND NOT column_record.attisdropped
            ORDER BY table_record.relname, column_record.attnum
            """
        )
        assert [tuple(row.values()) for row in columns] == [
            ("bindings", "key", "text", "pg_catalog", "ucs_basic"),
            ("bindings", "schema", "text", "pg_catalog", "ucs_basic"),
            (
                "bindings",
                "content_hash",
                "text",
                "pg_catalog",
                "ucs_basic",
            ),
            (
                "objects",
                "content_hash",
                "text",
                "pg_catalog",
                "ucs_basic",
            ),
            ("objects", "schema", "text", "pg_catalog", "ucs_basic"),
            ("objects", "canonical", "text", "pg_catalog", "ucs_basic"),
        ]

        constraints = await connection.fetch(
            """
            SELECT table_record.relname,
                constraint_record.contype::pg_catalog.text
                    AS constraint_type,
                pg_catalog.pg_get_constraintdef(constraint_record.oid)
                    AS definition
            FROM pg_catalog.pg_constraint AS constraint_record
            JOIN pg_catalog.pg_class AS table_record
                ON table_record.oid = constraint_record.conrelid
            JOIN pg_catalog.pg_namespace AS table_schema
                ON table_schema.oid = table_record.relnamespace
            WHERE table_schema.nspname = 'dr_store'
            ORDER BY table_record.relname,
                constraint_record.contype, constraint_record.conname
            """
        )
        assert not any(row["constraint_type"] == "f" for row in constraints)
        primary_keys = {
            row["relname"]: row["definition"]
            for row in constraints
            if row["constraint_type"] == "p"
        }
        assert primary_keys == {
            "bindings": "PRIMARY KEY (key)",
            "objects": "PRIMARY KEY (content_hash, schema)",
        }


async def test_exact_text_identity_and_lowercase_hash_checks(
    postgres_pool: asyncpg.Pool,
) -> None:
    await install_postgres(postgres_pool)
    composed = "\N{LATIN SMALL LETTER E WITH ACUTE}"
    decomposed = "e\N{COMBINING ACUTE ACCENT}"

    async with postgres_pool.acquire() as connection:
        for index, schema in enumerate(("Case", "case", composed, decomposed)):
            content_hash = f"{index:x}" * 64
            canonical = f'{{"schema":"{schema}"}}'
            await connection.execute(
                """
                INSERT INTO dr_store.objects
                    (content_hash, schema, canonical)
                VALUES ($1, $2, $3)
                """,
                content_hash,
                schema,
                canonical,
            )
            await connection.execute(
                """
                INSERT INTO dr_store.bindings (key, schema, content_hash)
                VALUES ($1, $2, $3)
                """,
                schema,
                schema,
                content_hash,
            )

        rows = await connection.fetch(
            """
            SELECT bindings.key, objects.schema, objects.canonical
            FROM dr_store.bindings
            JOIN dr_store.objects
                ON objects.content_hash = bindings.content_hash
                AND objects.schema = bindings.schema
            ORDER BY bindings.content_hash
            """
        )
        assert [tuple(row.values()) for row in rows] == [
            (schema, schema, canonical)
            for schema, canonical in (
                ("Case", '{"schema":"Case"}'),
                ("case", '{"schema":"case"}'),
                (composed, f'{{"schema":"{composed}"}}'),
                (decomposed, f'{{"schema":"{decomposed}"}}'),
            )
        ]

        for invalid_hash in ("A" * 64, "a" * 63, "g" * 64):
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    """
                    INSERT INTO dr_store.objects
                        (content_hash, schema, canonical)
                    VALUES ($1, 'invalid', '{}')
                    """,
                    invalid_hash,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    """
                    INSERT INTO dr_store.bindings
                        (key, schema, content_hash)
                    VALUES ($1, 'invalid', $2)
                    """,
                    invalid_hash,
                    invalid_hash,
                )


async def test_live_database_meets_supported_version_and_encoding(
    postgres_pool: asyncpg.Pool,
) -> None:
    async with postgres_pool.acquire() as connection:
        version_num = int(
            await connection.fetchval(
                "SELECT pg_catalog.current_setting('server_version_num')"
            )
        )
        encoding = await connection.fetchval(
            "SELECT pg_catalog.current_setting('server_encoding')"
        )

    assert 16 <= version_num // 10_000 <= 18
    assert encoding == "UTF8"
    await install_postgres(postgres_pool)


async def test_installer_does_not_stringify_retain_or_close_pool(
    postgres_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_stringification(_pool: asyncpg.Pool) -> str:
        pytest.fail("installer stringified the caller-owned pool")

    monkeypatch.setattr(asyncpg.Pool, "__str__", fail_stringification)
    await install_postgres(postgres_pool)

    assert not postgres_pool.is_closing()
    async with postgres_pool.acquire() as connection:
        assert await connection.fetchval("SELECT 1") == 1
