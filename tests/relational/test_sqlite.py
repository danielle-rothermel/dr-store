from __future__ import annotations

import sqlite3
import tempfile
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from dr_store.relational import (
    require_persisted_integer,
    require_persisted_text,
)
from dr_store.relational.errors import RelationalContractMismatchError
from dr_store.relational.sqlite import (
    connect_sqlite,
    create_component_metadata,
    raise_owned_table_inventory_mismatch,
    sqlite_owned_tables,
    sqlite_table_columns,
    verify_component_metadata,
    verify_sqlite_table,
)

_METADATA_TABLE = "dr_store_test_metadata"
_CREATE_METADATA = f"""
CREATE TABLE IF NOT EXISTS {_METADATA_TABLE} (
    component TEXT NOT NULL PRIMARY KEY CHECK (typeof(component) = 'text'),
    version INTEGER NOT NULL CHECK (
        typeof(version) = 'integer' AND version > 0
    )
)
"""
_METADATA_COLUMNS = (
    ("component", "TEXT", True, 1),
    ("version", "INTEGER", True, 0),
)


@pytest.fixture
def sqlite_path() -> Iterator[str]:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        yield handle.name


def test_metadata_idempotency(sqlite_path: str) -> None:
    connection = connect_sqlite(sqlite_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_CREATE_METADATA)
        verify_sqlite_table(
            connection,
            table=_METADATA_TABLE,
            create_sql=_CREATE_METADATA,
            columns=_METADATA_COLUMNS,
        )
        create_component_metadata(
            connection,
            metadata_table=_METADATA_TABLE,
            component="dr_store.test",
            version=1,
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        verify_component_metadata(
            connection,
            metadata_table=_METADATA_TABLE,
            component="dr_store.test",
            version=1,
        )
        create_component_metadata(
            connection,
            metadata_table=_METADATA_TABLE,
            component="dr_store.test",
            version=1,
        )
        connection.commit()
    finally:
        connection.close()


def test_version_mismatch(sqlite_path: str) -> None:
    connection = connect_sqlite(sqlite_path)
    try:
        connection.execute(_CREATE_METADATA)
        connection.execute(
            f"""
            INSERT INTO {_METADATA_TABLE} (component, version)
            VALUES ('dr_store.test', 99)
            """
        )
        with pytest.raises(RelationalContractMismatchError) as exc:
            verify_component_metadata(
                connection,
                metadata_table=_METADATA_TABLE,
                component="dr_store.test",
                version=1,
            )
        assert exc.value.aspect == "schema metadata"
    finally:
        connection.close()


def test_decode_guards() -> None:
    with pytest.raises(RelationalContractMismatchError):
        require_persisted_text(1, field="value")
    with pytest.raises(RelationalContractMismatchError):
        require_persisted_integer("1", field="value")


class _CountingObserver:
    def __init__(self) -> None:
        self.attempted = 0
        self.acquired = 0

    def transaction_attempted(self) -> None:
        self.attempted += 1

    def transaction_acquired(self) -> None:
        self.acquired += 1


def test_observer_hooks(sqlite_path: str) -> None:
    observer = _CountingObserver()
    connection = connect_sqlite(sqlite_path, observer=observer)
    try:
        connection.execute("BEGIN IMMEDIATE")
        observer.transaction_acquired()
        connection.commit()
        assert observer.attempted >= 1
        assert observer.acquired == 1
    finally:
        connection.close()


def test_sqlite_table_columns(sqlite_path: str) -> None:
    connection = sqlite3.connect(sqlite_path)
    connection.execute(_CREATE_METADATA)
    columns = sqlite_table_columns(connection, _METADATA_TABLE)
    assert columns == _METADATA_COLUMNS
    connection.close()


def test_owned_table_inventory(sqlite_path: str) -> None:
    connection = connect_sqlite(sqlite_path)
    try:
        connection.execute(_CREATE_METADATA)
        connection.execute(
            "CREATE TABLE extra (value TEXT NOT NULL PRIMARY KEY)"
        )
        tables = sqlite_owned_tables(connection, (_METADATA_TABLE, "extra"))
        assert tables == {_METADATA_TABLE, "extra"}
    finally:
        connection.close()


def test_shared_metadata_table_verifies_each_component(
    sqlite_path: str,
) -> None:
    connection = connect_sqlite(sqlite_path)
    try:
        connection.execute(_CREATE_METADATA)
        verify_sqlite_table(
            connection,
            table=_METADATA_TABLE,
            create_sql=_CREATE_METADATA,
            columns=_METADATA_COLUMNS,
        )
        create_component_metadata(
            connection,
            metadata_table=_METADATA_TABLE,
            component="dr_store.a",
            version=1,
        )
        create_component_metadata(
            connection,
            metadata_table=_METADATA_TABLE,
            component="dr_store.b",
            version=2,
        )
        verify_component_metadata(
            connection,
            metadata_table=_METADATA_TABLE,
            component="dr_store.a",
            version=1,
        )
        verify_component_metadata(
            connection,
            metadata_table=_METADATA_TABLE,
            component="dr_store.b",
            version=2,
        )
    finally:
        connection.close()


def test_column_drift_raises_mismatch(sqlite_path: str) -> None:
    connection = connect_sqlite(sqlite_path)
    try:
        connection.execute(
            f"""
            CREATE TABLE {_METADATA_TABLE} (
                component TEXT NOT NULL PRIMARY KEY,
                version TEXT NOT NULL
            )
            """
        )
        with pytest.raises(RelationalContractMismatchError) as exc:
            verify_sqlite_table(
                connection,
                table=_METADATA_TABLE,
                create_sql=_CREATE_METADATA,
                columns=_METADATA_COLUMNS,
            )
        assert exc.value.aspect == "columns"
    finally:
        connection.close()


def test_table_definition_drift_raises_mismatch(sqlite_path: str) -> None:
    connection = connect_sqlite(sqlite_path)
    try:
        connection.execute(
            f"""
            CREATE TABLE {_METADATA_TABLE} (
                component TEXT NOT NULL PRIMARY KEY,
                version INTEGER NOT NULL
            )
            """
        )
        with pytest.raises(RelationalContractMismatchError) as exc:
            verify_sqlite_table(
                connection,
                table=_METADATA_TABLE,
                create_sql=_CREATE_METADATA,
                columns=_METADATA_COLUMNS,
            )
        assert exc.value.aspect == "table definition"
    finally:
        connection.close()


def test_missing_table_raises_mismatch(sqlite_path: str) -> None:
    connection = connect_sqlite(sqlite_path)
    try:
        with pytest.raises(RelationalContractMismatchError) as exc:
            verify_sqlite_table(
                connection,
                table=_METADATA_TABLE,
                create_sql=_CREATE_METADATA,
                columns=_METADATA_COLUMNS,
            )
        assert exc.value.aspect == "columns"
    finally:
        connection.close()


def test_partial_owned_table_inventory_raises(sqlite_path: str) -> None:
    connection = connect_sqlite(sqlite_path)
    try:
        connection.execute(_CREATE_METADATA)
        tables = sqlite_owned_tables(connection, (_METADATA_TABLE, "missing"))
        with pytest.raises(RelationalContractMismatchError) as exc:
            raise_owned_table_inventory_mismatch(
                tables=tables,
                allowed=({_METADATA_TABLE, "missing"},),
            )
        assert exc.value.aspect == "owned table inventory"
    finally:
        connection.close()


def test_create_if_not_exists_normalization(sqlite_path: str) -> None:
    connection = connect_sqlite(sqlite_path)
    try:
        connection.execute(_CREATE_METADATA)
        verify_sqlite_table(
            connection,
            table=_METADATA_TABLE,
            create_sql=_CREATE_METADATA,
            columns=_METADATA_COLUMNS,
        )
    finally:
        connection.close()
