from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, cast

import psycopg.errors
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

from dr_store import install_postgres, install_postgres_sync
from dr_store.storage_backends import postgresql
from dr_store.storage_backends.postgresql_schema import POSTGRES_SCHEMA_FORMAT


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


async def test_rejects_non_engine_without_connection_work() -> None:
    with pytest.raises(
        TypeError,
        match=r"engine must be a sqlalchemy\.ext\.asyncio\.AsyncEngine",
    ):
        await install_postgres(cast("AsyncEngine", object()))


def test_rejects_non_sync_engine_without_connection_work() -> None:
    with pytest.raises(
        TypeError,
        match=r"engine must be a sqlalchemy\.engine\.Engine",
    ):
        install_postgres_sync(cast("Engine", object()))


def test_installer_has_no_credential_bearing_api_or_state() -> None:
    for installer in (install_postgres, install_postgres_sync):
        signature = inspect.signature(installer)
        assert list(signature.parameters) == ["engine"]
        assert installer.__closure__ is None
        assert installer.__dict__ == {}
    assert "dsn" not in inspect.getsource(postgresql).casefold()


def test_postgresql_schema_format_wire_literal_is_pinned() -> None:
    assert POSTGRES_SCHEMA_FORMAT == "dr-store-postgresql-v1"


async def test_installs_absent_fixed_schema_transactionally(
    postgres_engine: AsyncEngine,
) -> None:
    await install_postgres(postgres_engine)

    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SHOW search_path")) == "pg_catalog"
        )
        tables = await connection.scalar(
            text(
                """
                SELECT pg_catalog.array_agg(
                    tables.table_name ORDER BY tables.table_name
                )
                FROM information_schema.tables
                WHERE tables.table_schema = 'dr_store'
                """
            )
        )
        assert tables == "{bindings,objects,schema_format}"
        format_rows = [
            tuple(row.values())
            for row in (
                await connection.execute(
                    text(
                        "SELECT singleton, format FROM dr_store.schema_format"
                    )
                )
            ).mappings()
        ]
        assert format_rows == [(True, "dr-store-postgresql-v1")]


def test_installs_absent_fixed_schema_transactionally_sync(
    postgres_sync_engine: Engine,
) -> None:
    install_postgres_sync(postgres_sync_engine)

    with postgres_sync_engine.connect() as connection:
        assert connection.scalar(text("SHOW search_path")) == "pg_catalog"
        tables = connection.scalar(
            text(
                """
                SELECT pg_catalog.array_agg(
                    tables.table_name ORDER BY tables.table_name
                )
                FROM information_schema.tables
                WHERE tables.table_schema = 'dr_store'
                """
            )
        )
        assert tables == "{bindings,objects,schema_format}"
        format_rows = [
            tuple(row.values())
            for row in connection.execute(
                text("SELECT singleton, format FROM dr_store.schema_format")
            ).mappings()
        ]
        assert format_rows == [(True, "dr-store-postgresql-v1")]


async def test_installation_rolls_back_every_object_on_ddl_failure(
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_create(connection: object) -> None:
        from sqlalchemy import text
        from sqlalchemy.engine import Connection

        assert isinstance(connection, Connection)
        connection.execute(text("CREATE SCHEMA dr_store"))
        connection.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(
        postgresql,
        "_create_storage_tables_sync",
        failing_create,
    )

    with pytest.raises(DBAPIError):
        await install_postgres(postgres_engine)

    async with postgres_engine.connect() as connection:
        present = await connection.scalar(
            text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM pg_catalog.pg_namespace
                    WHERE pg_namespace.nspname = 'dr_store'
                )
                """
            )
        )
        assert present is False


def _is_duplicate_namespace_error(exc: BaseException) -> bool:
    if not isinstance(exc, DBAPIError):
        return False
    return isinstance(
        exc.orig,
        (psycopg.errors.DuplicateSchema, psycopg.errors.UniqueViolation),
    )


async def test_repeated_installation_fails_without_adoption(
    postgres_engine: AsyncEngine,
) -> None:
    await install_postgres(postgres_engine)

    with pytest.raises(DBAPIError) as caught:
        await install_postgres(postgres_engine)
    assert _is_duplicate_namespace_error(caught.value)


async def test_preexisting_namespace_is_not_adopted(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.begin() as connection:
        await connection.execute(text("CREATE SCHEMA dr_store"))

    with pytest.raises(DBAPIError) as caught:
        await install_postgres(postgres_engine)
    assert _is_duplicate_namespace_error(caught.value)

    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    """
                    SELECT pg_catalog.count(*)
                    FROM information_schema.tables
                    WHERE tables.table_schema = 'dr_store'
                    """
                )
            )
            == 0
        )


async def test_concurrent_installation_has_one_success_and_one_failure(
    postgres_engine: AsyncEngine,
) -> None:
    results = await asyncio.gather(
        install_postgres(postgres_engine),
        install_postgres(postgres_engine),
        return_exceptions=True,
    )

    assert sum(result is None for result in results) == 1
    failures = [
        result for result in results if isinstance(result, BaseException)
    ]
    assert len(failures) == 1
    assert isinstance(failures[0], DBAPIError)
    assert _is_duplicate_namespace_error(failures[0])


async def test_catalog_pins_qualified_exact_text_and_hash_leading_keys(
    postgres_engine: AsyncEngine,
) -> None:
    await install_postgres(postgres_engine)

    async with postgres_engine.connect() as connection:
        columns = [
            tuple(row.values())
            for row in (
                await connection.execute(
                    text(
                        """
                        SELECT
                            table_record.relname,
                            column_record.attname,
                            pg_catalog.format_type(
                                column_record.atttypid,
                                column_record.atttypmod
                            ) AS type_name,
                            collation_schema.nspname AS collation_schema,
                            collation_record.collname AS collation_name
                        FROM pg_catalog.pg_attribute AS column_record
                        JOIN pg_catalog.pg_class AS table_record
                            ON table_record.oid = column_record.attrelid
                        JOIN pg_catalog.pg_namespace AS table_schema
                            ON table_schema.oid = table_record.relnamespace
                        JOIN pg_catalog.pg_collation AS collation_record
                            ON collation_record.oid =
                                column_record.attcollation
                        JOIN pg_catalog.pg_namespace AS collation_schema
                            ON collation_schema.oid =
                                collation_record.collnamespace
                        WHERE table_schema.nspname = 'dr_store'
                            AND table_record.relname IN (
                                'objects', 'bindings', 'schema_format'
                            )
                            AND column_record.attnum > 0
                            AND NOT column_record.attisdropped
                        ORDER BY table_record.relname, column_record.attnum
                        """
                    )
                )
            ).mappings()
        ]
        assert columns == [
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
            (
                "schema_format",
                "format",
                "text",
                "pg_catalog",
                "ucs_basic",
            ),
        ]

        constraints = [
            dict(row)
            for row in (
                await connection.execute(
                    text(
                        """
                        SELECT table_record.relname,
                            constraint_record.contype::pg_catalog.text
                                AS constraint_type,
                            pg_catalog.pg_get_constraintdef(
                                constraint_record.oid
                            ) AS definition
                        FROM pg_catalog.pg_constraint AS constraint_record
                        JOIN pg_catalog.pg_class AS table_record
                            ON table_record.oid = constraint_record.conrelid
                        JOIN pg_catalog.pg_namespace AS table_schema
                            ON table_schema.oid = table_record.relnamespace
                        WHERE table_schema.nspname = 'dr_store'
                        ORDER BY table_record.relname,
                            constraint_record.contype,
                            constraint_record.conname
                        """
                    )
                )
            ).mappings()
        ]
        assert not any(row["constraint_type"] == "f" for row in constraints)
        primary_keys = {
            row["relname"]: row["definition"]
            for row in constraints
            if row["constraint_type"] == "p"
        }
        assert primary_keys == {
            "bindings": "PRIMARY KEY (key)",
            "objects": "PRIMARY KEY (content_hash, schema)",
            "schema_format": "PRIMARY KEY (singleton)",
        }


async def test_exact_text_identity_and_lowercase_hash_checks(
    postgres_engine: AsyncEngine,
) -> None:
    await install_postgres(postgres_engine)
    composed = "\N{LATIN SMALL LETTER E WITH ACUTE}"
    decomposed = "e\N{COMBINING ACUTE ACCENT}"

    async with postgres_engine.begin() as connection:
        for index, schema in enumerate(("Case", "case", composed, decomposed)):
            content_hash = f"{index:x}" * 64
            canonical = f'{{"schema":"{schema}"}}'
            await connection.execute(
                text(
                    """
                    INSERT INTO dr_store.objects
                        (content_hash, schema, canonical)
                    VALUES (:content_hash, :schema, :canonical)
                    """
                ),
                {
                    "content_hash": content_hash,
                    "schema": schema,
                    "canonical": canonical,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO dr_store.bindings (key, schema, content_hash)
                    VALUES (:key, :schema, :content_hash)
                    """
                ),
                {
                    "key": schema,
                    "schema": schema,
                    "content_hash": content_hash,
                },
            )

        rows = [
            tuple(row.values())
            for row in (
                await connection.execute(
                    text(
                        """
                        SELECT bindings.key, objects.schema, objects.canonical
                        FROM dr_store.bindings
                        JOIN dr_store.objects
                            ON objects.content_hash = bindings.content_hash
                            AND objects.schema = bindings.schema
                        ORDER BY bindings.content_hash
                        """
                    )
                )
            ).mappings()
        ]
        assert rows == [
            (schema, schema, canonical)
            for schema, canonical in (
                ("Case", '{"schema":"Case"}'),
                ("case", '{"schema":"case"}'),
                (composed, f'{{"schema":"{composed}"}}'),
                (decomposed, f'{{"schema":"{decomposed}"}}'),
            )
        ]

        for invalid_hash in ("A" * 64, "a" * 63, "g" * 64):
            with pytest.raises(DBAPIError) as object_error:
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """
                            INSERT INTO dr_store.objects
                                (content_hash, schema, canonical)
                            VALUES (:content_hash, 'invalid', '{}')
                            """
                        ),
                        {"content_hash": invalid_hash},
                    )
            assert isinstance(
                object_error.value.orig, psycopg.errors.CheckViolation
            )
            with pytest.raises(DBAPIError) as binding_error:
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """
                            INSERT INTO dr_store.bindings
                                (key, schema, content_hash)
                            VALUES (:key, 'invalid', :content_hash)
                            """
                        ),
                        {"key": invalid_hash, "content_hash": invalid_hash},
                    )
            assert isinstance(
                binding_error.value.orig, psycopg.errors.CheckViolation
            )


async def test_live_database_meets_supported_version_and_encoding(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        version_num = int(
            await connection.scalar(
                text("SELECT pg_catalog.current_setting('server_version_num')")
            )
        )
        encoding = await connection.scalar(
            text("SELECT pg_catalog.current_setting('server_encoding')")
        )

    assert 16 <= version_num // 10_000 <= 18
    assert encoding == "UTF8"
    await install_postgres(postgres_engine)


async def test_installer_does_not_stringify_retain_or_dispose_engine(
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_stringification(_engine: AsyncEngine) -> str:
        pytest.fail("installer stringified the caller-owned engine")

    monkeypatch.setattr(AsyncEngine, "__str__", fail_stringification)
    await install_postgres(postgres_engine)

    async with postgres_engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1
