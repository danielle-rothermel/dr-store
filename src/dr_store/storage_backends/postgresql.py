from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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
from dr_store.storage_backends.postgresql_schema import (
    POSTGRES_METADATA,
    POSTGRES_SCHEMA_FORMAT,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping

__all__ = [
    "POSTGRES_METADATA",
    "POSTGRES_SCHEMA_FORMAT",
    "PostgresBackend",
    "install_postgres",
    "install_postgres_sync",
]

_MINIMUM_POSTGRES_MAJOR = 16
_MAXIMUM_POSTGRES_MAJOR = 18
_DEFAULT_BATCH_CHUNK_SIZE = 512

_DATABASE_REQUIREMENTS_SQL = """
SELECT
    pg_catalog.current_setting('server_version_num')::pg_catalog.int4
        AS server_version_num,
    pg_catalog.current_setting('server_encoding') AS server_encoding
"""

_CREATE_SCHEMA_SQL = "CREATE SCHEMA dr_store"

_INSERT_SCHEMA_FORMAT_SQL = """
INSERT INTO dr_store.schema_format (singleton, format)
VALUES (TRUE, :format)
"""

_GET_SCHEMA_FORMAT_SQL = """
SELECT format
FROM dr_store.schema_format
"""

_INSERT_OBJECT_SQL = """
INSERT INTO dr_store.objects (content_hash, schema, canonical)
VALUES (:content_hash, :schema, :canonical)
ON CONFLICT (content_hash, schema) DO NOTHING
RETURNING schema, canonical
"""

_GET_OBJECT_SQL = """
SELECT schema, canonical
FROM dr_store.objects
WHERE content_hash = :content_hash
ORDER BY (schema = :schema) DESC
LIMIT 1
"""

_GET_EXACT_OBJECT_SQL = """
SELECT schema, canonical
FROM dr_store.objects
WHERE content_hash = :content_hash AND schema = :schema
"""

_INSERT_BINDING_SQL = """
INSERT INTO dr_store.bindings (key, schema, content_hash)
VALUES (:key, :schema, :content_hash)
ON CONFLICT (key) DO NOTHING
RETURNING key
"""

_GET_BINDING_SQL = """
SELECT schema, content_hash
FROM dr_store.bindings
WHERE key = :key
"""

_INSERT_OBJECTS_SQL = """
INSERT INTO dr_store.objects (content_hash, schema, canonical)
SELECT proposed.content_hash, proposed.schema, proposed.canonical
FROM ROWS FROM (
    pg_catalog.unnest(CAST(:content_hashes AS pg_catalog.text[])),
    pg_catalog.unnest(CAST(:schemas AS pg_catalog.text[])),
    pg_catalog.unnest(CAST(:canonicals AS pg_catalog.text[]))
) AS proposed(content_hash, schema, canonical)
ON CONFLICT (content_hash, schema) DO NOTHING
"""

_FETCH_OBJECTS_SQL = """
SELECT objects.content_hash, objects.schema, objects.canonical
FROM dr_store.objects AS objects
JOIN ROWS FROM (
    pg_catalog.unnest(CAST(:content_hashes AS pg_catalog.text[])),
    pg_catalog.unnest(CAST(:schemas AS pg_catalog.text[]))
) AS requested(content_hash, schema)
    ON objects.content_hash = requested.content_hash
    AND objects.schema = requested.schema
"""

_INSERT_BINDINGS_SQL = """
INSERT INTO dr_store.bindings (key, schema, content_hash)
SELECT proposed.key, proposed.schema, proposed.content_hash
FROM ROWS FROM (
    pg_catalog.unnest(CAST(:keys AS pg_catalog.text[])),
    pg_catalog.unnest(CAST(:schemas AS pg_catalog.text[])),
    pg_catalog.unnest(CAST(:content_hashes AS pg_catalog.text[]))
) AS proposed(key, schema, content_hash)
ON CONFLICT (key) DO NOTHING
RETURNING key
"""

_FETCH_BINDINGS_SQL = """
SELECT key, schema, content_hash
FROM dr_store.bindings
WHERE key = ANY(CAST(:keys AS pg_catalog.text[]))
"""

_DELETE_BINDINGS_SQL = """
DELETE FROM dr_store.bindings
WHERE key = ANY(CAST(:keys AS pg_catalog.text[]))
RETURNING key
"""


def _validate_database(*, version_num: int, server_encoding: str) -> None:
    major = version_num // 10_000
    if not _MINIMUM_POSTGRES_MAJOR <= major <= _MAXIMUM_POSTGRES_MAJOR:
        raise RuntimeError("PostgreSQL 16 through 18 is required")
    if server_encoding != "UTF8":
        raise RuntimeError("PostgreSQL server encoding must be UTF-8")


def _validate_schema_format(formats: list[object]) -> None:
    if formats != [POSTGRES_SCHEMA_FORMAT]:
        raise RuntimeError(
            "PostgreSQL schema format marker is missing, malformed, or "
            "unsupported"
        )


def _validate_enlisted_connection(connection: object) -> Connection:
    if not isinstance(connection, Connection):
        raise TypeError("connection must be a sqlalchemy.engine.Connection")
    return connection


def _fetchrow_sync(
    connection: Connection,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    result = connection.execute(text(query), parameters or {})
    return cast("Mapping[str, Any] | None", result.mappings().first())


def _fetch_sync(
    connection: Connection,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> list[Mapping[str, Any]]:
    result = connection.execute(text(query), parameters or {})
    return cast("list[Mapping[str, Any]]", list(result.mappings()))


def _execute_sync(
    connection: Connection,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> None:
    connection.execute(text(query), parameters or {})


def _put_object_on_connection(
    connection: Connection,
    *,
    schema: str,
    content_hash: str,
    canonical: str,
) -> PutOutcome:
    inserted = _fetchrow_sync(
        connection,
        _INSERT_OBJECT_SQL,
        {
            "content_hash": content_hash,
            "schema": schema,
            "canonical": canonical,
        },
    )
    if inserted is not None:
        return PutOutcome(
            inserted=True,
            stored_canonical=inserted["canonical"],
        )
    stored = _fetchrow_sync(
        connection,
        _GET_EXACT_OBJECT_SQL,
        {"content_hash": content_hash, "schema": schema},
    )
    assert stored is not None
    return PutOutcome(
        inserted=False,
        stored_canonical=stored["canonical"],
    )


def _get_object_on_connection(
    connection: Connection,
    *,
    schema: str,
    content_hash: str,
) -> tuple[str, str] | None:
    row = _fetchrow_sync(
        connection,
        _GET_OBJECT_SQL,
        {"content_hash": content_hash, "schema": schema},
    )
    if row is None:
        return None
    return (row["schema"], row["canonical"])


def _bind_on_connection(
    connection: Connection,
    *,
    key: str,
    schema: str,
    content_hash: str,
) -> BindOutcome:
    inserted = _fetchrow_sync(
        connection,
        _INSERT_BINDING_SQL,
        {"key": key, "schema": schema, "content_hash": content_hash},
    )
    row = _fetchrow_sync(connection, _GET_BINDING_SQL, {"key": key})
    assert row is not None
    return BindOutcome(
        bound=inserted is not None,
        existing_schema=row["schema"],
        existing_content_hash=row["content_hash"],
    )


def _get_binding_on_connection(
    connection: Connection,
    *,
    key: str,
) -> tuple[str, str] | None:
    row = _fetchrow_sync(connection, _GET_BINDING_SQL, {"key": key})
    if row is None:
        return None
    return (row["schema"], row["content_hash"])


def _chunked[T](
    values: tuple[T, ...],
    *,
    batch_chunk_size: int,
) -> Iterator[tuple[T, ...]]:
    for start in range(0, len(values), batch_chunk_size):
        yield values[start : start + batch_chunk_size]


def _get_bound_objects_on_connection(
    connection: Connection,
    *,
    keys: tuple[str, ...],
    batch_chunk_size: int,
) -> dict[str, BoundObjectRow]:
    bindings: dict[str, tuple[str, str]] = {}
    objects: dict[tuple[str, str], str] = {}
    for chunk in _chunked(keys, batch_chunk_size=batch_chunk_size):
        rows = _fetch_sync(
            connection,
            _FETCH_BINDINGS_SQL,
            {"keys": list(chunk)},
        )
        for row in rows:
            bindings[row["key"]] = (
                row["schema"],
                row["content_hash"],
            )

    references = tuple(dict.fromkeys(bindings.values()))
    for chunk in _chunked(references, batch_chunk_size=batch_chunk_size):
        rows = _fetch_sync(
            connection,
            _FETCH_OBJECTS_SQL,
            {
                "content_hashes": [reference[1] for reference in chunk],
                "schemas": [reference[0] for reference in chunk],
            },
        )
        for row in rows:
            objects[(row["schema"], row["content_hash"])] = row["canonical"]

    results: dict[str, BoundObjectRow] = {}
    for key, (schema, content_hash) in bindings.items():
        results[key] = BoundObjectRow(
            binding_schema=schema,
            binding_content_hash=content_hash,
            canonical=objects.get((schema, content_hash)),
        )
    return results


def _delete_bindings_on_connection(
    connection: Connection,
    *,
    keys: tuple[str, ...],
    batch_chunk_size: int,
) -> set[str]:
    deleted: set[str] = set()
    for chunk in _chunked(keys, batch_chunk_size=batch_chunk_size):
        rows = _fetch_sync(
            connection,
            _DELETE_BINDINGS_SQL,
            {"keys": list(chunk)},
        )
        deleted.update(row["key"] for row in rows)
    return deleted


def _put_bound_objects_on_connection(
    connection: Connection,
    *,
    entries: tuple[BoundObjectWrite, ...],
    batch_chunk_size: int,
) -> dict[str, BindOutcome]:
    objects: dict[tuple[str, str], str] = {}
    writes: dict[str, BoundObjectWrite] = {}
    for entry in entries:
        reference = (entry.schema, entry.content_hash)
        proposed = objects.setdefault(reference, entry.canonical)
        if proposed != entry.canonical:
            raise ObjectConflictError(
                schema=entry.schema,
                content_hash=entry.content_hash,
            )
        writes.setdefault(entry.key, entry)

    references = tuple(objects)
    inserted_keys: set[str] = set()
    stored_objects: dict[tuple[str, str], str] = {}
    stored_bindings: dict[str, tuple[str, str]] = {}
    for chunk in _chunked(references, batch_chunk_size=batch_chunk_size):
        _execute_sync(
            connection,
            _INSERT_OBJECTS_SQL,
            {
                "content_hashes": [reference[1] for reference in chunk],
                "schemas": [reference[0] for reference in chunk],
                "canonicals": [objects[reference] for reference in chunk],
            },
        )
    for chunk in _chunked(references, batch_chunk_size=batch_chunk_size):
        rows = _fetch_sync(
            connection,
            _FETCH_OBJECTS_SQL,
            {
                "content_hashes": [reference[1] for reference in chunk],
                "schemas": [reference[0] for reference in chunk],
            },
        )
        for row in rows:
            stored_objects[(row["schema"], row["content_hash"])] = row[
                "canonical"
            ]

    for reference, canonical in objects.items():
        assert reference in stored_objects
        if stored_objects[reference] != canonical:
            raise ObjectConflictError(
                schema=reference[0],
                content_hash=reference[1],
            )

    distinct_writes = tuple(writes.values())
    for chunk in _chunked(distinct_writes, batch_chunk_size=batch_chunk_size):
        rows = _fetch_sync(
            connection,
            _INSERT_BINDINGS_SQL,
            {
                "keys": [entry.key for entry in chunk],
                "schemas": [entry.schema for entry in chunk],
                "content_hashes": [entry.content_hash for entry in chunk],
            },
        )
        inserted_keys.update(row["key"] for row in rows)
    for chunk in _chunked(tuple(writes), batch_chunk_size=batch_chunk_size):
        rows = _fetch_sync(
            connection,
            _FETCH_BINDINGS_SQL,
            {"keys": list(chunk)},
        )
        for row in rows:
            stored_bindings[row["key"]] = (
                row["schema"],
                row["content_hash"],
            )

    assert stored_bindings.keys() == writes.keys()
    return {
        key: BindOutcome(
            bound=key in inserted_keys,
            existing_schema=stored_bindings[key][0],
            existing_content_hash=stored_bindings[key][1],
        )
        for key in writes
    }


def _create_storage_tables_sync(connection: Connection) -> None:
    connection.execute(text(_CREATE_SCHEMA_SQL))
    POSTGRES_METADATA.create_all(connection)


def _install_on_connection(connection: Connection) -> None:
    requirements = _fetchrow_sync(connection, _DATABASE_REQUIREMENTS_SQL)
    assert requirements is not None
    _validate_database(
        version_num=requirements["server_version_num"],
        server_encoding=requirements["server_encoding"],
    )
    _create_storage_tables_sync(connection)
    _execute_sync(
        connection,
        _INSERT_SCHEMA_FORMAT_SQL,
        {"format": POSTGRES_SCHEMA_FORMAT},
    )


def _validate_schema_on_connection(connection: Connection) -> None:
    rows = _fetch_sync(connection, _GET_SCHEMA_FORMAT_SQL)
    _validate_schema_format([row["format"] for row in rows])


def install_postgres_sync(engine: Engine) -> None:
    """Install the fixed ``dr_store`` schema into an empty namespace.

    The caller owns ``engine``. This operation acquires and releases one
    connection without disposing the engine.
    """
    if not isinstance(engine, Engine):
        raise TypeError("engine must be a sqlalchemy.engine.Engine")

    with engine.connect() as connection, connection.begin():
        _install_on_connection(connection)


async def _run_connection_operation[T](
    engine: AsyncEngine,
    operation: Callable[[AsyncConnection], Awaitable[T]],
    *,
    write: bool,
) -> T:
    async with engine.connect() as acquired:
        if write:
            async with acquired.begin():
                return await operation(acquired)
        return await operation(acquired)


async def install_postgres(engine: AsyncEngine) -> None:
    """Install the fixed ``dr_store`` schema into an empty namespace.

    The caller owns ``engine``. This operation acquires and releases one
    connection without disposing the engine.
    """
    if not isinstance(engine, AsyncEngine):
        raise TypeError("engine must be a sqlalchemy.ext.asyncio.AsyncEngine")

    async def install(connection: AsyncConnection) -> None:
        await connection.run_sync(_install_on_connection)

    await _run_connection_operation(engine, install, write=True)


class PostgresBackend:
    """Shared PostgreSQL storage through a caller-owned engine."""

    _async_engine: AsyncEngine | None
    _sync_engine: Engine | None
    _batch_chunk_size: int

    def __init__(self, engine: AsyncEngine) -> None:
        del engine
        raise TypeError(
            "use 'PostgresBackend.open_sync(engine)' or "
            "'await PostgresBackend.open(engine)'"
        )

    @classmethod
    def open_sync(
        cls,
        engine: Engine,
        *,
        batch_chunk_size: int = _DEFAULT_BATCH_CHUNK_SIZE,
    ) -> Self:
        """Validate the installed schema format and use ``engine``.

        ``batch_chunk_size`` must be positive and bounds batch statement
        parameter count below the PostgreSQL driver limit.
        """
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a sqlalchemy.engine.Engine")
        if batch_chunk_size <= 0:
            raise ValueError("batch_chunk_size must be positive")

        with engine.connect() as connection:
            _validate_schema_on_connection(connection)

        self = object.__new__(cls)
        self._async_engine = None
        self._sync_engine = engine
        self._batch_chunk_size = batch_chunk_size
        return self

    @classmethod
    async def open(
        cls,
        engine: AsyncEngine,
        *,
        batch_chunk_size: int = _DEFAULT_BATCH_CHUNK_SIZE,
    ) -> Self:
        """Validate the installed schema format and use ``engine``.

        ``batch_chunk_size`` must be positive and bounds batch statement
        parameter count below the PostgreSQL driver limit.
        """
        if not isinstance(engine, AsyncEngine):
            raise TypeError(
                "engine must be a sqlalchemy.ext.asyncio.AsyncEngine"
            )
        if batch_chunk_size <= 0:
            raise ValueError("batch_chunk_size must be positive")

        async def validate(connection: AsyncConnection) -> None:
            await connection.run_sync(_validate_schema_on_connection)

        await _run_connection_operation(
            engine,
            validate,
            write=False,
        )
        self = object.__new__(cls)
        self._async_engine = engine
        self._sync_engine = None
        self._batch_chunk_size = batch_chunk_size
        return self

    def _require_async_engine(self) -> AsyncEngine:
        if self._async_engine is None:
            raise RuntimeError(
                "async PostgreSQL backend operations require "
                "'await PostgresBackend.open(async_engine)'"
            )
        return self._async_engine

    def _chunks[T](self, values: tuple[T, ...]) -> Iterator[tuple[T, ...]]:
        yield from _chunked(values, batch_chunk_size=self._batch_chunk_size)

    async def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        validate_reference_schema(schema)
        validate_content_hash(content_hash)

        async def put(conn: AsyncConnection) -> PutOutcome:
            return await conn.run_sync(
                lambda sync_connection: _put_object_on_connection(
                    sync_connection,
                    schema=schema,
                    content_hash=content_hash,
                    canonical=canonical,
                )
            )

        return await _run_connection_operation(
            self._require_async_engine(),
            put,
            write=True,
        )

    def put_object_enlisted(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
        connection: Connection,
    ) -> PutOutcome:
        validate_reference_schema(schema)
        validate_content_hash(content_hash)
        return _put_object_on_connection(
            _validate_enlisted_connection(connection),
            schema=schema,
            content_hash=content_hash,
            canonical=canonical,
        )

    async def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        validate_reference_schema(schema)
        validate_content_hash(content_hash)

        async def get(conn: AsyncConnection) -> tuple[str, str] | None:
            return await conn.run_sync(
                lambda sync_connection: _get_object_on_connection(
                    sync_connection,
                    schema=schema,
                    content_hash=content_hash,
                )
            )

        return await _run_connection_operation(
            self._require_async_engine(),
            get,
            write=False,
        )

    def get_object_enlisted(
        self,
        *,
        schema: str,
        content_hash: str,
        connection: Connection,
    ) -> tuple[str, str] | None:
        validate_reference_schema(schema)
        validate_content_hash(content_hash)
        return _get_object_on_connection(
            _validate_enlisted_connection(connection),
            schema=schema,
            content_hash=content_hash,
        )

    async def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        validate_binding_key(key)
        validate_reference_schema(schema)
        validate_content_hash(content_hash)

        async def bind_key(conn: AsyncConnection) -> BindOutcome:
            return await conn.run_sync(
                lambda sync_connection: _bind_on_connection(
                    sync_connection,
                    key=key,
                    schema=schema,
                    content_hash=content_hash,
                )
            )

        return await _run_connection_operation(
            self._require_async_engine(),
            bind_key,
            write=True,
        )

    def bind_enlisted(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
        connection: Connection,
    ) -> BindOutcome:
        validate_binding_key(key)
        validate_reference_schema(schema)
        validate_content_hash(content_hash)
        return _bind_on_connection(
            _validate_enlisted_connection(connection),
            key=key,
            schema=schema,
            content_hash=content_hash,
        )

    async def get_binding(self, *, key: str) -> tuple[str, str] | None:
        validate_binding_key(key)

        async def get(conn: AsyncConnection) -> tuple[str, str] | None:
            return await conn.run_sync(
                lambda sync_connection: _get_binding_on_connection(
                    sync_connection,
                    key=key,
                )
            )

        return await _run_connection_operation(
            self._require_async_engine(),
            get,
            write=False,
        )

    def get_binding_enlisted(
        self,
        *,
        key: str,
        connection: Connection,
    ) -> tuple[str, str] | None:
        validate_binding_key(key)
        return _get_binding_on_connection(
            _validate_enlisted_connection(connection),
            key=key,
        )

    async def get_bound_objects(
        self,
        *,
        keys: tuple[str, ...],
    ) -> dict[str, BoundObjectRow]:
        for key in keys:
            validate_binding_key(key)
        distinct_keys = tuple(dict.fromkeys(keys))
        if not distinct_keys:
            return {}

        async def get(conn: AsyncConnection) -> dict[str, BoundObjectRow]:
            return await conn.run_sync(
                lambda sync_connection: _get_bound_objects_on_connection(
                    sync_connection,
                    keys=distinct_keys,
                    batch_chunk_size=self._batch_chunk_size,
                )
            )

        return await _run_connection_operation(
            self._require_async_engine(),
            get,
            write=False,
        )

    def get_bound_objects_enlisted(
        self,
        *,
        keys: tuple[str, ...],
        connection: Connection,
    ) -> dict[str, BoundObjectRow]:
        for key in keys:
            validate_binding_key(key)
        distinct_keys = tuple(dict.fromkeys(keys))
        if not distinct_keys:
            return {}
        return _get_bound_objects_on_connection(
            _validate_enlisted_connection(connection),
            keys=distinct_keys,
            batch_chunk_size=self._batch_chunk_size,
        )

    async def delete_bindings(self, *, keys: tuple[str, ...]) -> set[str]:
        for key in keys:
            validate_binding_key(key)
        distinct_keys = tuple(dict.fromkeys(keys))
        if not distinct_keys:
            return set()

        async def delete(conn: AsyncConnection) -> set[str]:
            return await conn.run_sync(
                lambda sync_connection: _delete_bindings_on_connection(
                    sync_connection,
                    keys=distinct_keys,
                    batch_chunk_size=self._batch_chunk_size,
                )
            )

        return await _run_connection_operation(
            self._require_async_engine(),
            delete,
            write=True,
        )

    async def put_bound_objects(
        self,
        *,
        entries: tuple[BoundObjectWrite, ...],
    ) -> dict[str, BindOutcome]:
        for entry in entries:
            validate_binding_key(entry.key)
            validate_reference_schema(entry.schema)
            validate_content_hash(entry.content_hash)
        if not entries:
            return {}

        async def put(conn: AsyncConnection) -> dict[str, BindOutcome]:
            return await conn.run_sync(
                lambda sync_connection: _put_bound_objects_on_connection(
                    sync_connection,
                    entries=entries,
                    batch_chunk_size=self._batch_chunk_size,
                )
            )

        return await _run_connection_operation(
            self._require_async_engine(),
            put,
            write=True,
        )

    def put_bound_objects_enlisted(
        self,
        *,
        entries: tuple[BoundObjectWrite, ...],
        connection: Connection,
    ) -> dict[str, BindOutcome]:
        for entry in entries:
            validate_binding_key(entry.key)
            validate_reference_schema(entry.schema)
            validate_content_hash(entry.content_hash)
        if not entries:
            return {}
        return _put_bound_objects_on_connection(
            _validate_enlisted_connection(connection),
            entries=entries,
            batch_chunk_size=self._batch_chunk_size,
        )
