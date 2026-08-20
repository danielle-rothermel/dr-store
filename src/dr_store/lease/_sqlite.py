from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from dr_store.lease._schema import reraise_schema_mismatch
from dr_store.lease._storage import (
    _T,
    _decode_row,
    _require_persisted_text,
    _row_insert_values,
    _row_match_values,
    _row_update_values,
    _Transition,
)
from dr_store.lease.models import (
    _AuthorityCorruptionError,
    _require_utc,
)
from dr_store.relational.errors import RelationalContractMismatchError
from dr_store.relational.sqlite import (
    connect_sqlite,
    create_component_metadata,
    raise_owned_table_inventory_mismatch,
    sqlite_owned_tables,
    verify_component_metadata,
    verify_sqlite_table,
)

if TYPE_CHECKING:
    from pathlib import Path

    from dr_store.relational.observer import TransactionObserver

_TABLE_NAME = "dr_store_lease_authority"
_METADATA_TABLE_NAME = "dr_store_lease_authority_metadata"
_COMPONENT = "dr_store.lease"
_SCHEMA_VERSION = 1
_SQLITE_MINIMUM_LEASE_DURATION = timedelta(milliseconds=1)

_SQLITE_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
    semantic_key TEXT PRIMARY KEY CHECK (typeof(semantic_key) = 'text'),
    request_hash TEXT NOT NULL CHECK (
        typeof(request_hash) = 'text'
    ),
    replay_policy TEXT NOT NULL CHECK (
        typeof(replay_policy) = 'text'
        AND replay_policy IN ('idempotent', 'durable_workflow', 'no_redrive')
    ),
    state TEXT NOT NULL CHECK (
        typeof(state) = 'text'
        AND state IN (
            'leased', 'succeeded', 'failed', 'recovery_required'
        )
    ),
    owner_id TEXT NOT NULL CHECK (typeof(owner_id) = 'text'),
    attempt_id TEXT NOT NULL CHECK (typeof(attempt_id) = 'text'),
    fence INTEGER NOT NULL CHECK (typeof(fence) = 'integer' AND fence > 0),
    expires_at TEXT CHECK (
        expires_at IS NULL OR typeof(expires_at) = 'text'
    ),
    terminal_json TEXT CHECK (
        terminal_json IS NULL OR typeof(terminal_json) = 'text'
    ),
    CHECK (
        (state = 'leased' AND expires_at IS NOT NULL
            AND terminal_json IS NULL)
        OR
        (state != 'leased' AND expires_at IS NULL
            AND terminal_json IS NOT NULL)
    )
)
"""

_SQLITE_CREATE_METADATA_TABLE = f"""
CREATE TABLE IF NOT EXISTS {_METADATA_TABLE_NAME} (
    component TEXT NOT NULL PRIMARY KEY CHECK (typeof(component) = 'text'),
    version INTEGER NOT NULL CHECK (
        typeof(version) = 'integer' AND version > 0
    )
)
"""

_SQLITE_TABLE_COLUMNS = (
    ("semantic_key", "TEXT", False, 1),
    ("request_hash", "TEXT", True, 0),
    ("replay_policy", "TEXT", True, 0),
    ("state", "TEXT", True, 0),
    ("owner_id", "TEXT", True, 0),
    ("attempt_id", "TEXT", True, 0),
    ("fence", "INTEGER", True, 0),
    ("expires_at", "TEXT", False, 0),
    ("terminal_json", "TEXT", False, 0),
)
_SQLITE_METADATA_COLUMNS = (
    ("component", "TEXT", True, 1),
    ("version", "INTEGER", True, 0),
)

_SELECT_ROW_SQLITE = f"""
SELECT request_hash, replay_policy, state, owner_id, attempt_id,
       fence, expires_at, terminal_json
FROM {_TABLE_NAME}
WHERE semantic_key = ?
"""

_INSERT_ROW_SQLITE = f"""
INSERT INTO {_TABLE_NAME} (
    semantic_key, request_hash, replay_policy, state, owner_id,
    attempt_id, fence, expires_at, terminal_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_ROW_SQLITE = f"""
UPDATE {_TABLE_NAME}
SET request_hash = ?, replay_policy = ?, state = ?, owner_id = ?,
    attempt_id = ?, fence = ?, expires_at = ?, terminal_json = ?
WHERE semantic_key = ?
  AND request_hash = ?
  AND replay_policy = ?
  AND state = ?
  AND owner_id = ?
  AND attempt_id = ?
  AND fence = ?
  AND expires_at IS ?
  AND terminal_json IS ?
"""

_SQLITE_SELECT_NOW = """
SELECT strftime('%Y-%m-%dT%H:%M:%f000+00:00', 'now')
"""

_OWNED_TABLES = (_TABLE_NAME, _METADATA_TABLE_NAME)


class _SQLiteStore:
    def __init__(
        self,
        path: str | Path,
        *,
        transaction_observer: TransactionObserver | None = None,
    ) -> None:
        raw_path = str(path)
        if not raw_path:
            raise ValueError("SQLite path must be non-empty")
        if raw_path == ":memory:":
            raise ValueError(
                "use LeaseAuthority.memory() for process-local memory"
            )
        self._path = raw_path
        self._transaction_observer = transaction_observer

    def initialize(self) -> None:
        # initialize() is deliberately unobserved: schema setup is not an
        # authority transaction and must not count toward observer hooks.
        connection = connect_sqlite(self._path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            tables = sqlite_owned_tables(connection, _OWNED_TABLES)
            if not tables:
                connection.execute(_SQLITE_CREATE_TABLE)
                connection.execute(_SQLITE_CREATE_METADATA_TABLE)
                create_component_metadata(
                    connection,
                    metadata_table=_METADATA_TABLE_NAME,
                    component=_COMPONENT,
                    version=_SCHEMA_VERSION,
                )
            elif tables != set(_OWNED_TABLES):
                raise_owned_table_inventory_mismatch(
                    tables=tables,
                    allowed=(set(_OWNED_TABLES),),
                )
            verify_sqlite_table(
                connection,
                table=_TABLE_NAME,
                create_sql=_SQLITE_CREATE_TABLE,
                columns=_SQLITE_TABLE_COLUMNS,
            )
            verify_sqlite_table(
                connection,
                table=_METADATA_TABLE_NAME,
                create_sql=_SQLITE_CREATE_METADATA_TABLE,
                columns=_SQLITE_METADATA_COLUMNS,
            )
            verify_component_metadata(
                connection,
                metadata_table=_METADATA_TABLE_NAME,
                component=_COMPONENT,
                version=_SCHEMA_VERSION,
            )
            connection.commit()
        except RelationalContractMismatchError as exc:
            connection.rollback()
            reraise_schema_mismatch(exc)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def validate_lease_duration(self, duration: timedelta) -> timedelta:
        if duration < _SQLITE_MINIMUM_LEASE_DURATION:
            raise ValueError(
                "lease_duration must be at least 1 millisecond for SQLite "
                "authority clock precision"
            )
        return duration

    def transaction(
        self,
        semantic_key: str,
        transition: _Transition[_T],
    ) -> _T:
        connection = connect_sqlite(
            self._path,
            observer=self._transaction_observer,
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            if self._transaction_observer is not None:
                self._transaction_observer.transaction_acquired()
            now_raw = connection.execute(_SQLITE_SELECT_NOW).fetchone()
            if now_raw is None:
                raise _AuthorityCorruptionError(
                    "SQLite did not return authority time"
                )
            now_text = _require_persisted_text(
                now_raw[0], field="SQLite authority time"
            )
            now = _require_utc(
                datetime.fromisoformat(now_text),
                field="SQLite authority time",
            )
            raw = connection.execute(
                _SELECT_ROW_SQLITE, (semantic_key,)
            ).fetchone()
            original = _decode_row(semantic_key, raw)
            updated, result = transition(original, now)
            if updated != original:
                if original is None:
                    if updated is None:
                        raise _AuthorityCorruptionError(
                            "transition removed an absent row"
                        )
                    connection.execute(
                        _INSERT_ROW_SQLITE,
                        _row_insert_values(updated),
                    )
                elif updated is None:
                    raise _AuthorityCorruptionError(
                        "authority rows cannot be deleted"
                    )
                else:
                    cursor = connection.execute(
                        _UPDATE_ROW_SQLITE,
                        (
                            *_row_update_values(updated),
                            semantic_key,
                            *_row_match_values(original),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise _AuthorityCorruptionError(
                            "conditional SQLite authority update lost"
                        )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        pass
