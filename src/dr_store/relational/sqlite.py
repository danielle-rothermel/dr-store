from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from dr_store.relational._helpers import (
    SqliteColumnContract,
    is_exact_component_version_row,
    normalized_sql,
    raise_owned_table_inventory_mismatch,
)
from dr_store.relational.errors import RelationalContractMismatchError

if TYPE_CHECKING:
    from dr_store.relational.observer import TransactionObserver


def connect_sqlite(
    path: str,
    *,
    observer: TransactionObserver | None = None,
) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 30000")
    if observer is not None:

        def authorize(
            action_code: int,
            argument_1: str | None,
            _argument_2: str | None,
            _database_name: str | None,
            _trigger_name: str | None,
        ) -> int:
            if (
                action_code == sqlite3.SQLITE_TRANSACTION
                and argument_1 == "BEGIN"
            ):
                observer.transaction_attempted()
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
    return connection


def sqlite_table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[SqliteColumnContract, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(
        (str(name), str(column_type), bool(not_null), int(primary_key))
        for _, name, column_type, not_null, _, primary_key in rows
    )


def verify_sqlite_table(
    connection: sqlite3.Connection,
    *,
    table: str,
    create_sql: str,
    columns: tuple[SqliteColumnContract, ...],
) -> None:
    actual_columns = sqlite_table_columns(connection, table)
    if actual_columns != columns:
        raise RelationalContractMismatchError(
            table=table,
            aspect="columns",
            expected=columns,
            actual=actual_columns,
        )
    raw_sql = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table,),
    ).fetchone()
    actual_sql = (
        None
        if raw_sql is None or not isinstance(raw_sql[0], str)
        else normalized_sql(raw_sql[0])
    )
    expected_sql = normalized_sql(create_sql).replace(
        "CREATE TABLE IF NOT EXISTS",
        "CREATE TABLE",
        1,
    )
    if actual_sql != expected_sql:
        raise RelationalContractMismatchError(
            table=table,
            aspect="table definition",
            expected=expected_sql,
            actual=actual_sql,
        )


def sqlite_owned_tables(
    connection: sqlite3.Connection,
    names: tuple[str, ...],
) -> set[str]:
    placeholders = ", ".join("?" for _ in names)
    rows = connection.execute(
        f"""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name IN ({placeholders})
        ORDER BY name
        """,
        names,
    ).fetchall()
    if not all(len(row) == 1 and type(row[0]) is str for row in rows):
        raise RelationalContractMismatchError(
            table="<catalog>",
            aspect="owned table inventory",
            expected="text table names",
            actual=rows,
        )
    return {row[0] for row in rows}


def create_component_metadata(
    connection: sqlite3.Connection,
    *,
    metadata_table: str,
    component: str,
    version: int,
) -> None:
    row = connection.execute(
        f"""
        SELECT component, version FROM {metadata_table}
        WHERE component = ?
        """,
        (component,),
    ).fetchone()
    if row is None:
        connection.execute(
            f"""
            INSERT INTO {metadata_table} (component, version)
            VALUES (?, ?)
            """,
            (component, version),
        )
        return
    if not is_exact_component_version_row(
        row, component=component, version=version
    ):
        raise RelationalContractMismatchError(
            table=metadata_table,
            aspect="schema version",
            expected=(component, version),
            actual=row,
        )


def verify_component_metadata(
    connection: sqlite3.Connection,
    *,
    metadata_table: str,
    component: str,
    version: int,
) -> None:
    row = connection.execute(
        f"""
        SELECT component, version FROM {metadata_table}
        WHERE component = ?
        """,
        (component,),
    ).fetchone()
    if not is_exact_component_version_row(
        row, component=component, version=version
    ):
        raise RelationalContractMismatchError(
            table=metadata_table,
            aspect="schema metadata",
            expected=(component, version),
            actual=row,
        )


__all__ = [
    "connect_sqlite",
    "create_component_metadata",
    "raise_owned_table_inventory_mismatch",
    "sqlite_owned_tables",
    "sqlite_table_columns",
    "verify_component_metadata",
    "verify_sqlite_table",
]
