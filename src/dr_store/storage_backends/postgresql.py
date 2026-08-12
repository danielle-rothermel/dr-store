from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection  # noqa: TC002
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


async def _fetchrow(
    connection: AsyncConnection,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    result = await connection.execute(text(query), parameters or {})
    return cast("Mapping[str, Any] | None", result.mappings().first())


async def _fetch(
    connection: AsyncConnection,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> list[Mapping[str, Any]]:
    result = await connection.execute(text(query), parameters or {})
    return cast("list[Mapping[str, Any]]", list(result.mappings()))


async def _execute(
    connection: AsyncConnection,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> None:
    await connection.execute(text(query), parameters or {})


async def _create_storage_tables(connection: AsyncConnection) -> None:
    await connection.execute(text(_CREATE_SCHEMA_SQL))

    def create_all(sync_connection: Connection) -> None:
        POSTGRES_METADATA.create_all(sync_connection)

    await connection.run_sync(create_all)


async def _run_connection_operation[T](
    engine: AsyncEngine,
    operation: Callable[[AsyncConnection], Awaitable[T]],
    *,
    transactional: bool,
    connection: AsyncConnection | None = None,
) -> T:
    if connection is not None:
        if not isinstance(connection, AsyncConnection):
            raise TypeError(
                "connection must be a sqlalchemy.ext.asyncio.AsyncConnection"
            )
        return await operation(connection)

    async with engine.connect() as acquired:
        if transactional:
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
        requirements = await _fetchrow(connection, _DATABASE_REQUIREMENTS_SQL)
        assert requirements is not None
        _validate_database(
            version_num=requirements["server_version_num"],
            server_encoding=requirements["server_encoding"],
        )
        await _create_storage_tables(connection)
        await _execute(
            connection,
            _INSERT_SCHEMA_FORMAT_SQL,
            {"format": POSTGRES_SCHEMA_FORMAT},
        )

    await _run_connection_operation(engine, install, transactional=True)


class PostgresBackend:
    """Shared PostgreSQL storage through a caller-owned asynchronous engine."""

    _engine: AsyncEngine
    _batch_chunk_size: int

    def __init__(self, engine: AsyncEngine) -> None:
        del engine
        raise TypeError("use 'await PostgresBackend.open(engine)'")

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
            rows = await _fetch(connection, _GET_SCHEMA_FORMAT_SQL)
            _validate_schema_format([row["format"] for row in rows])

        await _run_connection_operation(
            engine,
            validate,
            transactional=False,
        )
        self = object.__new__(cls)
        self._engine = engine
        self._batch_chunk_size = batch_chunk_size
        return self

    def _chunks[T](self, values: tuple[T, ...]) -> Iterator[tuple[T, ...]]:
        for start in range(0, len(values), self._batch_chunk_size):
            yield values[start : start + self._batch_chunk_size]

    async def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
        connection: AsyncConnection | None = None,
    ) -> PutOutcome:
        validate_reference_schema(schema)
        validate_content_hash(content_hash)

        async def put(conn: AsyncConnection) -> PutOutcome:
            inserted = await _fetchrow(
                conn,
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
            stored = await _fetchrow(
                conn,
                _GET_EXACT_OBJECT_SQL,
                {"content_hash": content_hash, "schema": schema},
            )
            assert stored is not None
            return PutOutcome(
                inserted=False,
                stored_canonical=stored["canonical"],
            )

        return await _run_connection_operation(
            self._engine,
            put,
            transactional=True,
            connection=connection,
        )

    async def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
        connection: AsyncConnection | None = None,
    ) -> tuple[str, str] | None:
        validate_reference_schema(schema)
        validate_content_hash(content_hash)

        async def get(conn: AsyncConnection) -> tuple[str, str] | None:
            row = await _fetchrow(
                conn,
                _GET_OBJECT_SQL,
                {"content_hash": content_hash, "schema": schema},
            )
            if row is None:
                return None
            return (row["schema"], row["canonical"])

        return await _run_connection_operation(
            self._engine,
            get,
            transactional=False,
            connection=connection,
        )

    async def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
        connection: AsyncConnection | None = None,
    ) -> BindOutcome:
        validate_binding_key(key)
        validate_reference_schema(schema)
        validate_content_hash(content_hash)

        async def bind_key(conn: AsyncConnection) -> BindOutcome:
            inserted = await _fetchrow(
                conn,
                _INSERT_BINDING_SQL,
                {"key": key, "schema": schema, "content_hash": content_hash},
            )
            row = await _fetchrow(conn, _GET_BINDING_SQL, {"key": key})
            assert row is not None
            return BindOutcome(
                bound=inserted is not None,
                existing_schema=row["schema"],
                existing_content_hash=row["content_hash"],
            )

        return await _run_connection_operation(
            self._engine,
            bind_key,
            transactional=True,
            connection=connection,
        )

    async def get_binding(
        self,
        *,
        key: str,
        connection: AsyncConnection | None = None,
    ) -> tuple[str, str] | None:
        validate_binding_key(key)

        async def get(conn: AsyncConnection) -> tuple[str, str] | None:
            row = await _fetchrow(conn, _GET_BINDING_SQL, {"key": key})
            if row is None:
                return None
            return (row["schema"], row["content_hash"])

        return await _run_connection_operation(
            self._engine,
            get,
            transactional=False,
            connection=connection,
        )

    async def get_bound_objects(
        self,
        *,
        keys: tuple[str, ...],
        connection: AsyncConnection | None = None,
    ) -> dict[str, BoundObjectRow]:
        for key in keys:
            validate_binding_key(key)
        distinct_keys = tuple(dict.fromkeys(keys))
        if not distinct_keys:
            return {}

        async def get(conn: AsyncConnection) -> dict[str, BoundObjectRow]:
            bindings: dict[str, tuple[str, str]] = {}
            objects: dict[tuple[str, str], str] = {}
            for chunk in self._chunks(distinct_keys):
                rows = await _fetch(
                    conn,
                    _FETCH_BINDINGS_SQL,
                    {"keys": list(chunk)},
                )
                for row in rows:
                    bindings[row["key"]] = (
                        row["schema"],
                        row["content_hash"],
                    )

            references = tuple(dict.fromkeys(bindings.values()))
            for chunk in self._chunks(references):
                rows = await _fetch(
                    conn,
                    _FETCH_OBJECTS_SQL,
                    {
                        "content_hashes": [
                            reference[1] for reference in chunk
                        ],
                        "schemas": [reference[0] for reference in chunk],
                    },
                )
                for row in rows:
                    objects[(row["schema"], row["content_hash"])] = row[
                        "canonical"
                    ]

            results: dict[str, BoundObjectRow] = {}
            for key, (schema, content_hash) in bindings.items():
                results[key] = BoundObjectRow(
                    binding_schema=schema,
                    binding_content_hash=content_hash,
                    canonical=objects.get((schema, content_hash)),
                )
            return results

        return await _run_connection_operation(
            self._engine,
            get,
            transactional=False,
            connection=connection,
        )

    async def put_bound_objects(
        self,
        *,
        entries: tuple[BoundObjectWrite, ...],
        connection: AsyncConnection | None = None,
    ) -> dict[str, BindOutcome]:
        for entry in entries:
            validate_binding_key(entry.key)
            validate_reference_schema(entry.schema)
            validate_content_hash(entry.content_hash)
        if not entries:
            return {}

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
        distinct_writes = tuple(writes.values())

        async def put(conn: AsyncConnection) -> dict[str, BindOutcome]:
            inserted_keys: set[str] = set()
            stored_objects: dict[tuple[str, str], str] = {}
            stored_bindings: dict[str, tuple[str, str]] = {}
            for chunk in self._chunks(references):
                await _execute(
                    conn,
                    _INSERT_OBJECTS_SQL,
                    {
                        "content_hashes": [
                            reference[1] for reference in chunk
                        ],
                        "schemas": [reference[0] for reference in chunk],
                        "canonicals": [
                            objects[reference] for reference in chunk
                        ],
                    },
                )
            for chunk in self._chunks(references):
                rows = await _fetch(
                    conn,
                    _FETCH_OBJECTS_SQL,
                    {
                        "content_hashes": [
                            reference[1] for reference in chunk
                        ],
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

            for chunk in self._chunks(distinct_writes):
                rows = await _fetch(
                    conn,
                    _INSERT_BINDINGS_SQL,
                    {
                        "keys": [entry.key for entry in chunk],
                        "schemas": [entry.schema for entry in chunk],
                        "content_hashes": [
                            entry.content_hash for entry in chunk
                        ],
                    },
                )
                inserted_keys.update(row["key"] for row in rows)
            for chunk in self._chunks(tuple(writes)):
                rows = await _fetch(
                    conn,
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

        return await _run_connection_operation(
            self._engine,
            put,
            transactional=True,
            connection=connection,
        )
