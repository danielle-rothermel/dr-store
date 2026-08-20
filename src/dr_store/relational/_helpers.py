from __future__ import annotations

from typing import Any

from dr_store.relational.errors import RelationalContractMismatchError

type SqliteColumnContract = tuple[str, str, bool, int]
type PostgresColumnContract = tuple[
    str,
    str,
    bool,
    int,
    str | None,
    str | None,
    str | None,
    bool | None,
    int | None,
]
type PostgresConstraintContract = tuple[
    str,
    str,
    tuple[str, ...],
    str | None,
    bool,
    bool,
    bool,
    bool,
]


def require_persisted_text(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise RelationalContractMismatchError(
            table="<row>",
            aspect=field,
            expected="text storage",
            actual=type(value).__name__,
        )
    return value


def require_persisted_integer(value: object, *, field: str) -> int:
    if type(value) is not int:
        raise RelationalContractMismatchError(
            table="<row>",
            aspect=field,
            expected="integer storage",
            actual=type(value).__name__,
        )
    return value


def normalized_sql(value: str) -> str:
    return " ".join(value.split())


def is_exact_component_version_row(
    row: tuple[Any, ...] | None,
    *,
    component: str,
    version: int,
) -> bool:
    return (
        row is not None
        and len(row) == 2
        and type(row[0]) is str
        and type(row[1]) is int
        and row == (component, version)
    )


def raise_owned_table_inventory_mismatch(
    *,
    tables: set[str],
    allowed: tuple[set[str], ...],
) -> None:
    raise RelationalContractMismatchError(
        table="<database>",
        aspect="owned table inventory",
        expected=allowed,
        actual=tables,
    )
