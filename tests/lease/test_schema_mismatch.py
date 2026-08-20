from __future__ import annotations

import sqlite3
import tempfile

import pytest

from dr_store.lease import (
    LeaseAuthority,
    LeaseAuthorityError,
    LeaseAuthoritySchemaMismatchError,
)
from dr_store.relational.errors import RelationalContractMismatchError


@pytest.fixture
def drifted_sqlite_path() -> str:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE dr_store_lease_authority (
                semantic_key TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                replay_policy TEXT NOT NULL,
                state TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                fence TEXT NOT NULL,
                expires_at TEXT,
                terminal_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE dr_store_lease_authority_metadata (
                component TEXT NOT NULL PRIMARY KEY,
                version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO dr_store_lease_authority_metadata (component, version)
            VALUES ('dr_store.lease', 1)
            """
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_drifted_sqlite_raises_schema_mismatch_error(
    drifted_sqlite_path: str,
) -> None:
    with pytest.raises(LeaseAuthoritySchemaMismatchError) as exc:
        LeaseAuthority.sqlite(drifted_sqlite_path)
    assert exc.value.aspect == "columns"


def test_schema_mismatch_is_caught_as_lease_authority_error(
    drifted_sqlite_path: str,
) -> None:
    with pytest.raises(LeaseAuthorityError):
        LeaseAuthority.sqlite(drifted_sqlite_path)


def test_schema_mismatch_is_not_relational_error_at_boundary(
    drifted_sqlite_path: str,
) -> None:
    with pytest.raises(LeaseAuthoritySchemaMismatchError):
        LeaseAuthority.sqlite(drifted_sqlite_path)
    with pytest.raises(LeaseAuthoritySchemaMismatchError) as exc:
        LeaseAuthority.sqlite(drifted_sqlite_path)
    assert not isinstance(exc.value, RelationalContractMismatchError)
