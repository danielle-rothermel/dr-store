"""Shared schema-contract infrastructure for typed-row persistence layers.

Consumers import dialect modules explicitly
(``dr_store.relational.sqlite``, ``dr_store.relational.postgres``) for
connection helpers, table introspection, and component metadata. The component
metadata helpers require ``component`` to be part of the table primary key.
"""

from __future__ import annotations

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
    "create_postgres_component_metadata",
    "create_sqlite_component_metadata",
    "postgres_table_columns",
    "postgres_table_constraints",
    "raise_owned_table_inventory_mismatch",
    "require_persisted_integer",
    "require_persisted_text",
    "sqlite_owned_tables",
    "sqlite_table_columns",
    "verify_postgres_component_metadata",
    "verify_postgres_table",
    "verify_sqlite_component_metadata",
    "verify_sqlite_table",
]
