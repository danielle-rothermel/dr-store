from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from psycopg import sql

from dr_store.relational.errors import RelationalContractMismatchError
from dr_store.relational.postgres import (
    create_component_metadata,
    postgres_table_columns,
    postgres_table_constraints,
    verify_component_metadata,
    verify_postgres_table,
)
from tests.conftest import require_postgres_dsn

if TYPE_CHECKING:
    from collections.abc import Iterator

require_postgres_dsn(_module_level=True)

_METADATA_TABLE = f"dr_store_test_metadata_{uuid.uuid4().hex}"
_METADATA_COLUMNS = (
    ("component", "text", True, 1, "pg_catalog", "C", "c", True, -1),
    ("version", "integer", True, 2, None, None, None, None, None),
)
_METADATA_CONSTRAINTS: tuple[
    tuple[str, str, tuple[str, ...], None, bool, bool, bool, bool], ...
] = (
    (
        _METADATA_TABLE,
        "p",
        ("component",),
        None,
        False,
        False,
        True,
        True,
    ),
)


@pytest.fixture
def postgres_connection() -> Iterator[object]:
    from psycopg import connect

    dsn = require_postgres_dsn()
    with connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    CREATE TABLE {} (
                        component TEXT COLLATE "C" PRIMARY KEY,
                        version INTEGER NOT NULL
                    )
                    """
                ).format(sql.Identifier(_METADATA_TABLE))
            )
        try:
            yield connection
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}").format(
                        sql.Identifier(_METADATA_TABLE)
                    )
                )


def test_postgres_table_columns(postgres_connection) -> None:
    columns = postgres_table_columns(postgres_connection, _METADATA_TABLE)
    assert columns == _METADATA_COLUMNS


def test_postgres_metadata_round_trip(postgres_connection) -> None:
    verify_postgres_table(
        postgres_connection,
        table=_METADATA_TABLE,
        columns=_METADATA_COLUMNS,
        constraints=_METADATA_CONSTRAINTS,
    )
    create_component_metadata(
        postgres_connection,
        metadata_table=_METADATA_TABLE,
        component="dr_store.test",
        version=1,
    )
    verify_component_metadata(
        postgres_connection,
        metadata_table=_METADATA_TABLE,
        component="dr_store.test",
        version=1,
    )


def test_postgres_metadata_scoped_by_component(postgres_connection) -> None:
    create_component_metadata(
        postgres_connection,
        metadata_table=_METADATA_TABLE,
        component="dr_store.a",
        version=1,
    )
    create_component_metadata(
        postgres_connection,
        metadata_table=_METADATA_TABLE,
        component="dr_store.b",
        version=2,
    )
    verify_component_metadata(
        postgres_connection,
        metadata_table=_METADATA_TABLE,
        component="dr_store.a",
        version=1,
    )
    verify_component_metadata(
        postgres_connection,
        metadata_table=_METADATA_TABLE,
        component="dr_store.b",
        version=2,
    )


def test_postgres_version_mismatch(postgres_connection) -> None:
    create_component_metadata(
        postgres_connection,
        metadata_table=_METADATA_TABLE,
        component="dr_store.test",
        version=1,
    )
    with postgres_connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                UPDATE {}
                SET version = 99
                WHERE component = %s
                """
            ).format(sql.Identifier(_METADATA_TABLE)),
            ("dr_store.test",),
        )
    with pytest.raises(RelationalContractMismatchError) as exc:
        verify_component_metadata(
            postgres_connection,
            metadata_table=_METADATA_TABLE,
            component="dr_store.test",
            version=1,
        )
    assert exc.value.aspect == "schema metadata"


def test_postgres_collation_drift(postgres_connection) -> None:
    drifted_table = f"{_METADATA_TABLE}_drift"
    with postgres_connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                CREATE TABLE {} (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            ).format(sql.Identifier(drifted_table))
        )
    try:
        drifted_constraints = (
            (
                drifted_table,
                "p",
                ("component",),
                None,
                False,
                False,
                True,
                True,
            ),
        )
        with pytest.raises(RelationalContractMismatchError) as exc:
            verify_postgres_table(
                postgres_connection,
                table=drifted_table,
                columns=_METADATA_COLUMNS,
                constraints=drifted_constraints,
            )
        assert exc.value.aspect == "columns"
    finally:
        with postgres_connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(
                    sql.Identifier(drifted_table)
                )
            )


def test_postgres_table_constraints(postgres_connection) -> None:
    constraints = postgres_table_constraints(
        postgres_connection,
        _METADATA_TABLE,
    )
    assert constraints == _METADATA_CONSTRAINTS
