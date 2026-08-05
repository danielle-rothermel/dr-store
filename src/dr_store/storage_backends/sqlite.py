from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

from dr_store.storage_backends.contract import BindOutcome, PutOutcome

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_BUSY_TIMEOUT_MS = 30_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    schema       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    PRIMARY KEY (schema, content_hash)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bindings (
    key          TEXT PRIMARY KEY NOT NULL,
    schema       TEXT NOT NULL,
    content_hash TEXT NOT NULL
) WITHOUT ROWID;
"""


class SqliteBackend:
    """Persistent SQLite object and binding storage.

    Initialize before concurrent use. Thereafter, per-thread connections
    coordinate object and binding operations across processes through SQLite.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        with self._immediate() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO objects "
                "(schema, content_hash, canonical) VALUES (?, ?, ?)",
                (schema, content_hash, canonical),
            )
            inserted = cursor.rowcount == 1
            row = conn.execute(
                "SELECT schema, canonical FROM objects "
                "WHERE schema = ? AND content_hash = ?",
                (schema, content_hash),
            ).fetchone()
            stored_schema, stored_canonical = row
        return PutOutcome(
            inserted=inserted,
            stored_schema=stored_schema,
            stored_canonical=stored_canonical,
        )

    def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        # Alternate-schema lookup distinguishes mismatch from missing content.
        row = self._conn.execute(
            "SELECT schema, canonical FROM objects "
            "WHERE content_hash = ? ORDER BY schema = ? DESC LIMIT 1",
            (content_hash, schema),
        ).fetchone()
        if row is None:
            return None
        return (row[0], row[1])

    def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        with self._immediate() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO bindings "
                "(key, schema, content_hash) VALUES (?, ?, ?)",
                (key, schema, content_hash),
            )
            bound = cursor.rowcount == 1
            row = conn.execute(
                "SELECT schema, content_hash FROM bindings WHERE key = ?",
                (key,),
            ).fetchone()
            existing_schema, existing_hash = row
        return BindOutcome(
            bound=bound,
            existing_schema=existing_schema,
            existing_content_hash=existing_hash,
        )

    def get_binding(self, *, key: str) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT schema, content_hash FROM bindings WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return (row[0], row[1])
