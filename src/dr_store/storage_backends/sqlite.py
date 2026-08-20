from __future__ import annotations

import asyncio
import enum
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Self

from dr_store.content_addressing import (
    validate_binding_key,
    validate_content_hash,
    validate_reference_schema,
)
from dr_store.core.errors import ObjectConflictError
from dr_store.storage_backends.contract import (
    BindOutcome,
    BoundObjectRow,
    BoundObjectWrite,
    PutOutcome,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import TracebackType

_BUSY_TIMEOUT_MS = 30_000
_KEY_QUERY_CHUNK_SIZE = 999
_TRANSIENT_DATABASE_PATHS = frozenset({"", ":memory:"})

# `DELETE ... RETURNING`, which binding deletion uses to report per-key
# outcomes from one statement, requires SQLite 3.35. The floor is checked once
# at open so an unsupported library fails there rather than only when an
# eviction is attempted.
MINIMUM_SQLITE_VERSION = (3, 35, 0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    schema       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    PRIMARY KEY (schema, content_hash)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS objects_by_content_hash
    ON objects (content_hash, schema);

CREATE TABLE IF NOT EXISTS bindings (
    key          TEXT PRIMARY KEY NOT NULL,
    schema       TEXT NOT NULL,
    content_hash TEXT NOT NULL
) WITHOUT ROWID;
"""


class _Lifecycle(enum.Enum):
    OPEN = enum.auto()
    CLOSING = enum.auto()
    CLOSED = enum.auto()
    FAILED = enum.auto()


def _persistent_database_path(path: str | Path) -> str:
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str):
        raise TypeError("SQLite database path must be text")
    if raw_path in _TRANSIENT_DATABASE_PATHS:
        raise ValueError(
            "SQLite backend requires a persistent filesystem path"
        )
    return str(Path(raw_path).absolute())


def _check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
        required = ".".join(str(part) for part in MINIMUM_SQLITE_VERSION)
        raise RuntimeError(
            f"SQLite backend requires SQLite {required} or later, "
            f"got {sqlite3.sqlite_version}"
        )


def _open_connection(path: str, *, busy_timeout_ms: int) -> sqlite3.Connection:
    _check_sqlite_version()
    connection = sqlite3.connect(
        path,
        timeout=busy_timeout_ms / 1000,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        connection.executescript(_SCHEMA)
    except BaseException:
        with suppress(Exception):
            connection.close()
        raise
    return connection


async def _await_settled[T](future: asyncio.Future[T]) -> T:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError as error:
            cancellation = error
            continue
        except BaseException as error:
            if cancellation is not None:
                raise cancellation from error
            raise
        if cancellation is not None:
            raise cancellation
        return result


class SqliteBackend:
    """One asynchronous worker and connection for persistent SQLite storage.

    Requires SQLite ``MINIMUM_SQLITE_VERSION`` or later; opening against an
    older library raises ``RuntimeError``.
    """

    _loop: asyncio.AbstractEventLoop
    _worker: ThreadPoolExecutor
    _connection: sqlite3.Connection
    _admission: asyncio.Lock
    _state: _Lifecycle
    _close_task: asyncio.Task[None] | None

    def __init__(self, path: str | Path) -> None:
        del path
        raise TypeError("use 'await SqliteBackend.open(path)'")

    @classmethod
    async def open(
        cls,
        path: str | Path,
        *,
        busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
    ) -> Self:
        """Open a persistent SQLite backend on ``path``.

        ``busy_timeout_ms`` configures SQLite lock waiting; when the bound
        expires, SQLite raises ``OperationalError`` rather than retrying
        indefinitely.
        """
        database_path = _persistent_database_path(path)
        loop = asyncio.get_running_loop()
        worker = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="dr-store-sqlite",
        )
        concurrent = worker.submit(
            _open_connection,
            database_path,
            busy_timeout_ms=busy_timeout_ms,
        )
        ready = asyncio.wrap_future(concurrent, loop=loop)
        try:
            connection = await _await_settled(ready)
        except asyncio.CancelledError:
            if (
                ready.done()
                and not ready.cancelled()
                and ready.exception() is None
            ):
                connection = await _await_settled(ready)
                closing = asyncio.wrap_future(
                    worker.submit(connection.close),
                    loop=loop,
                )
                with suppress(asyncio.CancelledError):
                    await _await_settled(closing)
            worker.shutdown(wait=True)
            raise
        except BaseException:
            worker.shutdown(wait=True)
            raise

        self = object.__new__(cls)
        self._loop = loop
        self._worker = worker
        self._connection = connection
        self._admission = asyncio.Lock()
        self._state = _Lifecycle.OPEN
        self._close_task = None
        return self

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError(
                "SQLite backend must be used on the event loop that opened it"
            )

    def _check_operation(self) -> None:
        self._check_loop()
        if self._state is not _Lifecycle.OPEN:
            raise RuntimeError("SQLite backend is closed")

    async def _run[T](
        self, operation: Callable[..., T], /, *args: object
    ) -> T:
        self._check_loop()
        async with self._admission:
            if self._state is not _Lifecycle.OPEN:
                raise RuntimeError("SQLite backend is closed")
            concurrent = self._worker.submit(operation, *args)
            future = asyncio.wrap_future(concurrent, loop=self._loop)
            return await _await_settled(future)

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    async def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        self._check_operation()
        validate_reference_schema(schema)
        validate_content_hash(content_hash)
        return await self._run(
            self._put_object,
            schema,
            content_hash,
            canonical,
        )

    def _put_object(
        self,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        with self._immediate() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO objects "
                "(schema, content_hash, canonical) VALUES (?, ?, ?)",
                (schema, content_hash, canonical),
            )
            inserted = cursor.rowcount == 1
            row = connection.execute(
                "SELECT schema, canonical FROM objects "
                "WHERE schema = ? AND content_hash = ?",
                (schema, content_hash),
            ).fetchone()
            assert row is not None
            _stored_schema, stored_canonical = row
        return PutOutcome(
            inserted=inserted,
            stored_canonical=stored_canonical,
        )

    async def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        self._check_operation()
        validate_reference_schema(schema)
        validate_content_hash(content_hash)
        return await self._run(self._get_object, schema, content_hash)

    def _get_object(
        self,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        row = self._connection.execute(
            "SELECT schema, canonical FROM objects "
            "WHERE content_hash = ? ORDER BY schema = ? DESC LIMIT 1",
            (content_hash, schema),
        ).fetchone()
        if row is None:
            return None
        return (row[0], row[1])

    async def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        self._check_operation()
        validate_binding_key(key)
        validate_reference_schema(schema)
        validate_content_hash(content_hash)
        return await self._run(self._bind, key, schema, content_hash)

    def _bind(
        self,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        with self._immediate() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO bindings "
                "(key, schema, content_hash) VALUES (?, ?, ?)",
                (key, schema, content_hash),
            )
            bound = cursor.rowcount == 1
            row = connection.execute(
                "SELECT schema, content_hash FROM bindings WHERE key = ?",
                (key,),
            ).fetchone()
            assert row is not None
            existing_schema, existing_hash = row
        return BindOutcome(
            bound=bound,
            existing_schema=existing_schema,
            existing_content_hash=existing_hash,
        )

    async def get_binding(self, *, key: str) -> tuple[str, str] | None:
        self._check_operation()
        validate_binding_key(key)
        return await self._run(self._get_binding, key)

    def _get_binding(self, key: str) -> tuple[str, str] | None:
        row = self._connection.execute(
            "SELECT schema, content_hash FROM bindings WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return (row[0], row[1])

    async def get_bound_objects(
        self,
        *,
        keys: tuple[str, ...],
    ) -> dict[str, BoundObjectRow]:
        self._check_operation()
        for key in keys:
            validate_binding_key(key)
        if not keys:
            return {}
        return await self._run(self._get_bound_objects, keys)

    def _get_bound_objects(
        self,
        keys: tuple[str, ...],
    ) -> dict[str, BoundObjectRow]:
        rows: dict[str, BoundObjectRow] = {}
        for start in range(0, len(keys), _KEY_QUERY_CHUNK_SIZE):
            chunk = keys[start : start + _KEY_QUERY_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            query = (
                "SELECT bindings.key, bindings.schema, "  # noqa: S608
                "bindings.content_hash, objects.schema, objects.canonical "
                "FROM bindings LEFT JOIN objects "
                "ON objects.schema = bindings.schema "
                "AND objects.content_hash = bindings.content_hash "
                f"WHERE bindings.key IN ({placeholders})"
            )
            stored_rows = self._connection.execute(query, chunk).fetchall()
            for (
                key,
                binding_schema,
                binding_content_hash,
                _object_schema,
                canonical,
            ) in stored_rows:
                rows[key] = BoundObjectRow(
                    binding_schema=binding_schema,
                    binding_content_hash=binding_content_hash,
                    canonical=canonical,
                )
        return rows

    async def delete_bindings(self, *, keys: tuple[str, ...]) -> set[str]:
        self._check_operation()
        for key in keys:
            validate_binding_key(key)
        if not keys:
            return set()
        return await self._run(self._delete_bindings, keys)

    def _delete_bindings(self, keys: tuple[str, ...]) -> set[str]:
        deleted: set[str] = set()
        with self._immediate() as connection:
            for start in range(0, len(keys), _KEY_QUERY_CHUNK_SIZE):
                chunk = keys[start : start + _KEY_QUERY_CHUNK_SIZE]
                placeholders = ", ".join("?" for _ in chunk)
                rows = connection.execute(
                    "DELETE FROM bindings "  # noqa: S608
                    f"WHERE key IN ({placeholders}) RETURNING key",
                    chunk,
                ).fetchall()
                deleted.update(row[0] for row in rows)
        return deleted

    async def put_bound_objects(
        self,
        *,
        entries: tuple[BoundObjectWrite, ...],
    ) -> dict[str, BindOutcome]:
        self._check_operation()
        for entry in entries:
            validate_binding_key(entry.key)
            validate_reference_schema(entry.schema)
            validate_content_hash(entry.content_hash)
        if not entries:
            return {}
        return await self._run(self._put_bound_objects, entries)

    def _put_bound_objects(
        self,
        entries: tuple[BoundObjectWrite, ...],
    ) -> dict[str, BindOutcome]:
        with self._immediate() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO objects "
                "(schema, content_hash, canonical) VALUES (?, ?, ?)",
                (
                    (entry.schema, entry.content_hash, entry.canonical)
                    for entry in entries
                ),
            )
            for entry in entries:
                row = connection.execute(
                    "SELECT canonical FROM objects "
                    "WHERE schema = ? AND content_hash = ?",
                    (entry.schema, entry.content_hash),
                ).fetchone()
                assert row is not None
                if row[0] != entry.canonical:
                    raise ObjectConflictError(
                        schema=entry.schema,
                        content_hash=entry.content_hash,
                    )

            outcomes: dict[str, BindOutcome] = {}
            for entry in entries:
                if entry.key in outcomes:
                    continue
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO bindings "
                    "(key, schema, content_hash) VALUES (?, ?, ?)",
                    (entry.key, entry.schema, entry.content_hash),
                )
                bound = cursor.rowcount == 1
                row = connection.execute(
                    "SELECT schema, content_hash FROM bindings WHERE key = ?",
                    (entry.key,),
                ).fetchone()
                assert row is not None
                outcomes[entry.key] = BindOutcome(
                    bound=bound,
                    existing_schema=row[0],
                    existing_content_hash=row[1],
                )
        return outcomes

    async def aclose(self) -> None:
        if self._state is _Lifecycle.CLOSED:
            return
        self._check_loop()
        if self._close_task is None:
            self._state = _Lifecycle.CLOSING
            self._close_task = self._loop.create_task(self._close_resources())
        await _await_settled(self._close_task)

    async def _close_resources(self) -> None:
        failure: BaseException | None = None
        try:
            async with self._admission:
                concurrent = self._worker.submit(self._connection.close)
                closing = asyncio.wrap_future(concurrent, loop=self._loop)
                await _await_settled(closing)
        except BaseException as error:
            failure = error
            raise
        finally:
            self._worker.shutdown(wait=True)
            self._state = (
                _Lifecycle.CLOSED if failure is None else _Lifecycle.FAILED
            )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, exc_value, traceback
        await self.aclose()
        return False
