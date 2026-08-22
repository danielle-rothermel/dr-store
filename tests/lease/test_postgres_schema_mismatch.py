from __future__ import annotations

import uuid
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from psycopg import sql

from dr_store.lease import (
    LeaseAuthority,
    LeaseAuthorityError,
    LeaseAuthoritySchemaMismatchError,
)
from dr_store.relational.errors import RelationalContractMismatchError
from tests.conftest import require_postgres_dsn

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

require_postgres_dsn(_module_level=True)

_TABLE = "dr_store_lease_authority"
_METADATA = "dr_store_lease_authority_metadata"

_CREATE_DRIFTED_FENCE_TYPE = sql.SQL(
    """
    CREATE TABLE {} (
        semantic_key TEXT COLLATE "C" PRIMARY KEY,
        request_hash TEXT COLLATE "C" NOT NULL,
        replay_policy TEXT COLLATE "C" NOT NULL CHECK (
            replay_policy IN ('idempotent', 'durable_workflow', 'no_redrive')
        ),
        state TEXT COLLATE "C" NOT NULL CHECK (
            state IN (
                'leased', 'succeeded', 'failed', 'recovery_required'
            )
        ),
        owner_id TEXT COLLATE "C" NOT NULL,
        attempt_id TEXT COLLATE "C" NOT NULL,
        fence TEXT COLLATE "C" NOT NULL,
        expires_at TEXT COLLATE "C",
        terminal_json TEXT COLLATE "C",
        CHECK (
            (state = 'leased' AND expires_at IS NOT NULL
                AND terminal_json IS NULL)
            OR
            (state != 'leased' AND expires_at IS NULL
                AND terminal_json IS NOT NULL)
        )
    )
    """
)
_CREATE_MISSING_FENCE_CHECK = sql.SQL(
    """
    CREATE TABLE {} (
        semantic_key TEXT COLLATE "C" PRIMARY KEY,
        request_hash TEXT COLLATE "C" NOT NULL,
        replay_policy TEXT COLLATE "C" NOT NULL CHECK (
            replay_policy IN ('idempotent', 'durable_workflow', 'no_redrive')
        ),
        state TEXT COLLATE "C" NOT NULL CHECK (
            state IN (
                'leased', 'succeeded', 'failed', 'recovery_required'
            )
        ),
        owner_id TEXT COLLATE "C" NOT NULL,
        attempt_id TEXT COLLATE "C" NOT NULL,
        fence BIGINT NOT NULL,
        expires_at TEXT COLLATE "C",
        terminal_json TEXT COLLATE "C",
        CHECK (
            (state = 'leased' AND expires_at IS NOT NULL
                AND terminal_json IS NULL)
            OR
            (state != 'leased' AND expires_at IS NULL
                AND terminal_json IS NOT NULL)
        )
    )
    """
)
_CREATE_MISSING_PRIMARY_KEY = sql.SQL(
    """
    CREATE TABLE {} (
        semantic_key TEXT COLLATE "C" NOT NULL,
        request_hash TEXT COLLATE "C" NOT NULL,
        replay_policy TEXT COLLATE "C" NOT NULL CHECK (
            replay_policy IN ('idempotent', 'durable_workflow', 'no_redrive')
        ),
        state TEXT COLLATE "C" NOT NULL CHECK (
            state IN (
                'leased', 'succeeded', 'failed', 'recovery_required'
            )
        ),
        owner_id TEXT COLLATE "C" NOT NULL,
        attempt_id TEXT COLLATE "C" NOT NULL,
        fence BIGINT NOT NULL CHECK (fence > 0),
        expires_at TEXT COLLATE "C",
        terminal_json TEXT COLLATE "C",
        CHECK (
            (state = 'leased' AND expires_at IS NOT NULL
                AND terminal_json IS NULL)
            OR
            (state != 'leased' AND expires_at IS NULL
                AND terminal_json IS NOT NULL)
        )
    )
    """
)


@pytest.fixture
def isolated_schema(postgres_dsn: str) -> Iterator[str]:
    from psycopg import connect

    schema = f"lease_mismatch_{uuid.uuid4().hex}"
    with connect(postgres_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    try:
        yield schema
    finally:
        with (
            connect(postgres_dsn) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def _schema_connect(
    schema: str,
) -> Callable[[str], AbstractContextManager[Any]]:
    @contextmanager
    def connect(dsn: str) -> Iterator[Any]:
        from psycopg import connect as psycopg_connect

        with psycopg_connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}, pg_catalog").format(
                        sql.Identifier(schema)
                    )
                )
            yield connection

    return connect


def _install_metadata(cursor: Any, schema: str) -> None:
    cursor.execute(
        sql.SQL(
            """
            CREATE TABLE {} (
                component TEXT COLLATE "C" PRIMARY KEY,
                version INTEGER NOT NULL
            )
            """
        ).format(sql.Identifier(schema, _METADATA))
    )
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {} (component, version)
            VALUES ('dr_store.lease', 1)
            """
        ).format(sql.Identifier(schema, _METADATA))
    )


def _install_drifted_tables(
    postgres_dsn: str, schema: str, create_lease: sql.SQL
) -> None:
    from psycopg import connect

    with connect(postgres_dsn) as connection, connection.cursor() as cursor:
        _install_metadata(cursor, schema)
        cursor.execute(create_lease.format(sql.Identifier(schema, _TABLE)))


def _open_authority(postgres_dsn: str, schema: str) -> LeaseAuthority:
    return LeaseAuthority.postgresql(
        postgres_dsn, _connect=_schema_connect(schema)
    )


def test_drifted_postgres_column_type_raises_schema_mismatch(
    postgres_dsn: str,
    isolated_schema: str,
) -> None:
    _install_drifted_tables(
        postgres_dsn, isolated_schema, _CREATE_DRIFTED_FENCE_TYPE
    )
    with pytest.raises(LeaseAuthoritySchemaMismatchError) as exc:
        _open_authority(postgres_dsn, isolated_schema)
    assert exc.value.table == _TABLE
    assert exc.value.aspect == "columns"


def test_missing_fence_check_names_constraint_aspect(
    postgres_dsn: str,
    isolated_schema: str,
) -> None:
    _install_drifted_tables(
        postgres_dsn, isolated_schema, _CREATE_MISSING_FENCE_CHECK
    )
    with pytest.raises(LeaseAuthoritySchemaMismatchError) as exc:
        _open_authority(postgres_dsn, isolated_schema)
    assert exc.value.table == _TABLE
    assert exc.value.aspect == "PRIMARY KEY and CHECK constraints"
    assert "fence > 0" in str(exc.value.actual)


def test_missing_primary_key_names_constraint_aspect(
    postgres_dsn: str,
    isolated_schema: str,
) -> None:
    _install_drifted_tables(
        postgres_dsn, isolated_schema, _CREATE_MISSING_PRIMARY_KEY
    )
    with pytest.raises(LeaseAuthoritySchemaMismatchError) as exc:
        _open_authority(postgres_dsn, isolated_schema)
    assert exc.value.table == _TABLE
    assert exc.value.aspect == "PRIMARY KEY and CHECK constraints"
    assert "PRIMARY KEY (semantic_key)" in str(exc.value.actual)


def test_postgres_schema_mismatch_is_caught_as_lease_authority_error(
    postgres_dsn: str,
    isolated_schema: str,
) -> None:
    _install_drifted_tables(
        postgres_dsn, isolated_schema, _CREATE_DRIFTED_FENCE_TYPE
    )
    with pytest.raises(LeaseAuthorityError):
        _open_authority(postgres_dsn, isolated_schema)


def test_postgres_schema_mismatch_is_not_relational_error_at_boundary(
    postgres_dsn: str,
    isolated_schema: str,
) -> None:
    _install_drifted_tables(
        postgres_dsn, isolated_schema, _CREATE_DRIFTED_FENCE_TYPE
    )
    with pytest.raises(LeaseAuthoritySchemaMismatchError) as exc:
        _open_authority(postgres_dsn, isolated_schema)
    assert isinstance(exc.value, LeaseAuthorityError)
    assert not isinstance(exc.value, RelationalContractMismatchError)
