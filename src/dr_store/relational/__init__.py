from __future__ import annotations

import sqlite3

from dr_store.relational._helpers import (
    require_persisted_integer,
    require_persisted_text,
)
from dr_store.relational.errors import RelationalContractMismatchError
from dr_store.relational.observer import TransactionObserver
from dr_store.relational.postgres import (
    ConnectFactory,
    postgres_table_columns,
    postgres_table_constraints,
    verify_postgres_table,
)
from dr_store.relational.postgres import (
    create_component_metadata as create_postgres_component_metadata,
)
from dr_store.relational.postgres import (
    verify_component_metadata as verify_postgres_component_metadata,
)
from dr_store.relational.sqlite import (
    connect_sqlite,
    raise_owned_table_inventory_mismatch,
    sqlite_owned_tables,
    sqlite_table_columns,
    verify_sqlite_table,
)
from dr_store.relational.sqlite import (
    create_component_metadata as create_sqlite_component_metadata,
)
from dr_store.relational.sqlite import (
    verify_component_metadata as verify_sqlite_component_metadata,
)

__all__ = [
    "ConnectFactory",
    "RelationalContractMismatchError",
    "TransactionObserver",
    "connect_sqlite",
    "create_component_metadata",
    "postgres_table_columns",
    "postgres_table_constraints",
    "raise_owned_table_inventory_mismatch",
    "require_persisted_integer",
    "require_persisted_text",
    "sqlite_owned_tables",
    "sqlite_table_columns",
    "verify_component_metadata",
    "verify_postgres_table",
    "verify_sqlite_table",
]


def create_component_metadata(
    connection: object,
    *,
    metadata_table: str,
    component: str,
    version: int,
) -> None:
    if isinstance(connection, sqlite3.Connection):
        create_sqlite_component_metadata(
            connection,
            metadata_table=metadata_table,
            component=component,
            version=version,
        )
        return
    create_postgres_component_metadata(
        connection,
        metadata_table=metadata_table,
        component=component,
        version=version,
    )


def verify_component_metadata(
    connection: object,
    *,
    metadata_table: str,
    component: str,
    version: int,
) -> None:
    if isinstance(connection, sqlite3.Connection):
        verify_sqlite_component_metadata(
            connection,
            metadata_table=metadata_table,
            component=component,
            version=version,
        )
        return
    verify_postgres_component_metadata(
        connection,
        metadata_table=metadata_table,
        component=component,
        version=version,
    )
