from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from dr_store.relational._helpers import (
    PostgresColumnContract,
    PostgresConstraintContract,
    is_exact_component_version_row,
)
from dr_store.relational.errors import RelationalContractMismatchError

type ConnectFactory = Callable[[str], AbstractContextManager[Any]]


_POSTGRES_COLUMNS_SQL = """
SELECT column_record.column_name,
       column_record.data_type,
       column_record.is_nullable,
       column_record.ordinal_position,
       collation_namespace.nspname,
       collation_record.collname,
       collation_record.collprovider,
       collation_record.collisdeterministic,
       collation_record.collencoding
FROM information_schema.columns AS column_record
JOIN pg_catalog.pg_namespace AS table_namespace
  ON table_namespace.nspname = column_record.table_schema
JOIN pg_catalog.pg_class AS table_record
  ON table_record.relnamespace = table_namespace.oid
 AND table_record.relname = column_record.table_name
JOIN pg_catalog.pg_attribute AS attribute_record
  ON attribute_record.attrelid = table_record.oid
 AND attribute_record.attname = column_record.column_name
LEFT JOIN pg_catalog.pg_collation AS collation_record
  ON collation_record.oid = attribute_record.attcollation
LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
  ON collation_namespace.oid = collation_record.collnamespace
WHERE column_record.table_schema = current_schema()
  AND column_record.table_name = %s
ORDER BY column_record.ordinal_position
"""

_POSTGRES_SELECT_CONSTRAINTS = """
SELECT cls.relname, constraint_record.contype,
       COALESCE(
           array_agg(attribute_record.attname::text ORDER BY key.ordinality)
               FILTER (WHERE attribute_record.attname IS NOT NULL),
           ARRAY[]::text[]
       ),
       CASE
           WHEN constraint_record.contype = 'c'
           THEN pg_get_expr(
               constraint_record.conbin,
               constraint_record.conrelid,
               true
           )
           ELSE NULL
       END,
       constraint_record.condeferrable,
       constraint_record.condeferred,
       constraint_record.convalidated,
       constraint_record.connoinherit
FROM pg_catalog.pg_constraint AS constraint_record
JOIN pg_catalog.pg_class AS cls
  ON cls.oid = constraint_record.conrelid
JOIN pg_catalog.pg_namespace AS namespace_record
  ON namespace_record.oid = cls.relnamespace
LEFT JOIN LATERAL unnest(constraint_record.conkey)
    WITH ORDINALITY AS key(attnum, ordinality)
  ON true
LEFT JOIN pg_catalog.pg_attribute AS attribute_record
  ON attribute_record.attrelid = constraint_record.conrelid
 AND attribute_record.attnum = key.attnum
WHERE namespace_record.nspname = current_schema()
  AND cls.relname = %s
  AND constraint_record.contype IN ('p', 'c')
GROUP BY cls.relname,
         constraint_record.contype,
         constraint_record.conname,
         constraint_record.conbin,
         constraint_record.conrelid,
         constraint_record.condeferrable,
         constraint_record.condeferred,
         constraint_record.convalidated,
         constraint_record.connoinherit
ORDER BY cls.relname, constraint_record.contype, constraint_record.conname
"""


def postgres_table_columns(
    connection: Any,
    table: str,
) -> tuple[PostgresColumnContract, ...]:
    with connection.cursor() as cursor:
        cursor.execute(_POSTGRES_COLUMNS_SQL, (table,))
        return tuple(
            (
                str(name),
                str(column_type),
                str(is_nullable) == "NO",
                int(ordinal_position),
                (None if collation_schema is None else str(collation_schema)),
                None if collation_name is None else str(collation_name),
                (
                    None
                    if collation_provider is None
                    else str(collation_provider)
                ),
                (
                    None
                    if collation_is_deterministic is None
                    else bool(collation_is_deterministic)
                ),
                (
                    None
                    if collation_encoding is None
                    else int(collation_encoding)
                ),
            )
            for (
                name,
                column_type,
                is_nullable,
                ordinal_position,
                collation_schema,
                collation_name,
                collation_provider,
                collation_is_deterministic,
                collation_encoding,
            ) in cursor.fetchall()
        )


def _parse_postgres_constraints(
    rows: list[tuple[Any, ...]],
) -> tuple[PostgresConstraintContract, ...]:
    constraints: list[PostgresConstraintContract] = []
    for row in rows:
        if len(row) != 8:
            raise RelationalContractMismatchError(
                table="<catalog>",
                aspect="constraint row shape",
                expected=8,
                actual=row,
            )
        (
            table_name,
            constraint_type,
            columns,
            expression,
            deferrable,
            deferred,
            validated,
            no_inherit,
        ) = row
        if not isinstance(columns, (list, tuple)) or not all(
            isinstance(column, str) for column in columns
        ):
            raise RelationalContractMismatchError(
                table=str(table_name),
                aspect="constrained columns",
                expected="a sequence of column names",
                actual=columns,
            )
        flags = (deferrable, deferred, validated, no_inherit)
        if not all(isinstance(flag, bool) for flag in flags):
            raise RelationalContractMismatchError(
                table=str(table_name),
                aspect="constraint flags",
                expected="four booleans",
                actual=flags,
            )
        constraints.append(
            (
                str(table_name),
                str(constraint_type),
                tuple(columns),
                None if expression is None else str(expression),
                deferrable,
                deferred,
                validated,
                no_inherit,
            )
        )
    return tuple(constraints)


def _describe_postgres_constraint(
    constraint: PostgresConstraintContract,
) -> str:
    (
        table,
        constraint_type,
        columns,
        expression,
        deferrable,
        deferred,
        validated,
        no_inherit,
    ) = constraint
    if constraint_type == "p":
        definition = f"PRIMARY KEY ({', '.join(columns)})"
    else:
        definition = f"CHECK ({expression}) on columns ({', '.join(columns)})"
    return (
        f"{table} {definition} [deferrable={deferrable}, "
        f"deferred={deferred}, validated={validated}, "
        f"no_inherit={no_inherit}]"
    )


def postgres_table_constraints(
    connection: Any,
    table: str,
) -> tuple[PostgresConstraintContract, ...]:
    with connection.cursor() as cursor:
        cursor.execute(_POSTGRES_SELECT_CONSTRAINTS, (table,))
        return _parse_postgres_constraints(cursor.fetchall())


def verify_postgres_table(
    connection: Any,
    *,
    table: str,
    columns: tuple[PostgresColumnContract, ...],
    constraints: tuple[PostgresConstraintContract, ...],
) -> None:
    actual_columns = postgres_table_columns(connection, table)
    if actual_columns != columns:
        raise RelationalContractMismatchError(
            table=table,
            aspect="columns",
            expected=columns,
            actual=actual_columns,
        )
    actual_constraints = list(postgres_table_constraints(connection, table))
    remaining = list(actual_constraints)
    missing: list[PostgresConstraintContract] = []
    for expected in constraints:
        if expected in remaining:
            remaining.remove(expected)
        else:
            missing.append(expected)
    if not missing and not remaining:
        return
    details: list[str] = []
    if missing:
        details.append(
            "missing "
            + "; ".join(
                _describe_postgres_constraint(constraint)
                for constraint in missing
            )
        )
    if remaining:
        details.append(
            "unexpected "
            + "; ".join(
                _describe_postgres_constraint(constraint)
                for constraint in remaining
            )
        )
    affected_tables = sorted(
        {constraint[0] for constraint in (*missing, *remaining)}
    )
    raise RelationalContractMismatchError(
        table=(
            affected_tables[0]
            if len(affected_tables) == 1
            else "<constraint catalog>"
        ),
        aspect="PRIMARY KEY and CHECK constraints",
        expected="; ".join(
            _describe_postgres_constraint(constraint)
            for constraint in constraints
        ),
        actual="; ".join(details),
    )


def create_component_metadata(
    connection: Any,
    *,
    metadata_table: str,
    component: str,
    version: int,
) -> None:
    """Record or verify one component/version pair.

    ``component`` must be the metadata table primary key. Duplicate rows for
    the same component are undefined and are not detected here.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT component, version FROM {metadata_table}
            WHERE component = %s
            """,
            (component,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                f"""
                INSERT INTO {metadata_table} (component, version)
                VALUES (%s, %s)
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
    connection: Any,
    *,
    metadata_table: str,
    component: str,
    version: int,
) -> None:
    """Verify one component/version pair.

    ``component`` must be the metadata table primary key. Duplicate rows for
    the same component are undefined and are not detected here.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT component, version FROM {metadata_table}
            WHERE component = %s
            """,
            (component,),
        )
        row = cursor.fetchone()
    if not is_exact_component_version_row(
        row, component=component, version=version
    ):
        raise RelationalContractMismatchError(
            table=metadata_table,
            aspect="schema metadata",
            expected=(component, version),
            actual=row,
        )
