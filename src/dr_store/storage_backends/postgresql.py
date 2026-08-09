from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self, cast

import asyncpg

from dr_store.content_addressing import (
    _validate_binding_key,
    _validate_content_hash,
    _validate_reference_schema,
)
from dr_store.core.errors import ObjectConflictError
from dr_store.storage_backends.contract import (
    BindOutcome,
    BoundObjectRow,
    BoundObjectWrite,
    PutOutcome,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

__all__ = ["PostgresBackend", "install_postgres"]

_MINIMUM_POSTGRES_MAJOR = 16
_MAXIMUM_POSTGRES_MAJOR = 18
_BATCH_CHUNK_SIZE = 512

# Persisted schema-format contract. A changed literal is a new format.
_POSTGRES_SCHEMA_FORMAT: Final = "dr-store-postgresql-v1"

_DATABASE_REQUIREMENTS_SQL = """
SELECT
    pg_catalog.current_setting('server_version_num')::pg_catalog.int4
        AS server_version_num,
    pg_catalog.current_setting('server_encoding') AS server_encoding
"""

_INSTALL_SQL = """
CREATE SCHEMA dr_store;

CREATE TABLE dr_store.schema_format (
    singleton pg_catalog.bool PRIMARY KEY,
    format pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    CONSTRAINT schema_format_singleton CHECK (singleton)
);

CREATE TABLE dr_store.objects (
    content_hash pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    schema pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    canonical pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    CONSTRAINT objects_content_hash_lowercase_hex CHECK (
        pg_catalog.octet_length(content_hash) = 64
        AND pg_catalog.translate(
            content_hash,
            '0123456789abcdef',
            ''
        ) = ''
    ),
    PRIMARY KEY (content_hash, schema)
);

CREATE TABLE dr_store.bindings (
    key pg_catalog.text COLLATE pg_catalog.ucs_basic PRIMARY KEY,
    schema pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    content_hash pg_catalog.text COLLATE pg_catalog.ucs_basic NOT NULL,
    CONSTRAINT bindings_content_hash_lowercase_hex CHECK (
        pg_catalog.octet_length(content_hash) = 64
        AND pg_catalog.translate(
            content_hash,
            '0123456789abcdef',
            ''
        ) = ''
    )
);
"""

_INSERT_SCHEMA_FORMAT_SQL = """
INSERT INTO dr_store.schema_format (singleton, format)
VALUES (TRUE, $1)
"""

_GET_SCHEMA_FORMAT_SQL = """
SELECT format
FROM dr_store.schema_format
"""

_INSERT_OBJECT_SQL = """
INSERT INTO dr_store.objects (content_hash, schema, canonical)
VALUES ($1, $2, $3)
ON CONFLICT (content_hash, schema) DO NOTHING
RETURNING schema, canonical
"""

_GET_OBJECT_SQL = """
SELECT schema, canonical
FROM dr_store.objects
WHERE content_hash = $1
ORDER BY (schema = $2) DESC
LIMIT 1
"""

_GET_EXACT_OBJECT_SQL = """
SELECT schema, canonical
FROM dr_store.objects
WHERE content_hash = $1 AND schema = $2
"""

_INSERT_BINDING_SQL = """
INSERT INTO dr_store.bindings (key, schema, content_hash)
VALUES ($1, $2, $3)
ON CONFLICT (key) DO NOTHING
RETURNING key
"""

_GET_BINDING_SQL = """
SELECT schema, content_hash
FROM dr_store.bindings
WHERE key = $1
"""

_INSERT_OBJECTS_SQL = """
INSERT INTO dr_store.objects (content_hash, schema, canonical)
SELECT proposed.content_hash, proposed.schema, proposed.canonical
FROM ROWS FROM (
    pg_catalog.unnest($1::pg_catalog.text[]),
    pg_catalog.unnest($2::pg_catalog.text[]),
    pg_catalog.unnest($3::pg_catalog.text[])
) AS proposed(content_hash, schema, canonical)
ON CONFLICT (content_hash, schema) DO NOTHING
"""

_FETCH_OBJECTS_SQL = """
SELECT objects.content_hash, objects.schema, objects.canonical
FROM dr_store.objects AS objects
JOIN ROWS FROM (
    pg_catalog.unnest($1::pg_catalog.text[]),
    pg_catalog.unnest($2::pg_catalog.text[])
) AS requested(content_hash, schema)
    ON objects.content_hash = requested.content_hash
    AND objects.schema = requested.schema
"""

_INSERT_BINDINGS_SQL = """
INSERT INTO dr_store.bindings (key, schema, content_hash)
SELECT proposed.key, proposed.schema, proposed.content_hash
FROM ROWS FROM (
    pg_catalog.unnest($1::pg_catalog.text[]),
    pg_catalog.unnest($2::pg_catalog.text[]),
    pg_catalog.unnest($3::pg_catalog.text[])
) AS proposed(key, schema, content_hash)
ON CONFLICT (key) DO NOTHING
RETURNING key
"""

_FETCH_BINDINGS_SQL = """
SELECT key, schema, content_hash
FROM dr_store.bindings
WHERE key = ANY($1::pg_catalog.text[])
"""


def _chunks[T](values: tuple[T, ...]) -> Iterator[tuple[T, ...]]:
    for start in range(0, len(values), _BATCH_CHUNK_SIZE):
        yield values[start : start + _BATCH_CHUNK_SIZE]


def _validate_database(*, version_num: int, server_encoding: str) -> None:
    major = version_num // 10_000
    if not _MINIMUM_POSTGRES_MAJOR <= major <= _MAXIMUM_POSTGRES_MAJOR:
        raise RuntimeError("PostgreSQL 16 through 18 is required")
    if server_encoding != "UTF8":
        raise RuntimeError("PostgreSQL server encoding must be UTF-8")


def _validate_schema_format(formats: list[object]) -> None:
    if formats != [_POSTGRES_SCHEMA_FORMAT]:
        raise RuntimeError(
            "PostgreSQL schema format marker is missing, malformed, or "
            "unsupported"
        )


_NO_RESULT = object()


@dataclass(frozen=True, slots=True)
class _Settled[T]:
    result: T | object = _NO_RESULT
    cancellation: asyncio.CancelledError | None = None
    failure: BaseException | None = None


async def _settle_task[T](
    task: asyncio.Task[T],
    *,
    cancel_on_cancellation: bool,
) -> _Settled[T]:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
                if cancel_on_cancellation and not task.done():
                    task.cancel()
            if task.done():
                if task.cancelled():
                    return _Settled(cancellation=cancellation)
                try:
                    result = task.result()
                except BaseException as task_error:  # noqa: BLE001
                    return _Settled(
                        cancellation=cancellation,
                        failure=task_error,
                    )
                return _Settled(
                    result=result,
                    cancellation=cancellation,
                )
            continue
        except BaseException as error:  # noqa: BLE001 - task boundary.
            return _Settled(cancellation=cancellation, failure=error)
        else:
            return _Settled(result=result, cancellation=cancellation)


async def _run_pool_operation[T](
    pool: asyncpg.Pool,
    operation: Callable[[asyncpg.Connection], Awaitable[T]],
    *,
    transactional: bool,
) -> T:
    connection = await pool.acquire()

    async def run() -> T:
        if transactional:
            async with connection.transaction():
                return await operation(connection)
        return await operation(connection)

    operation_settled = await _settle_task(
        asyncio.create_task(run()),
        cancel_on_cancellation=True,
    )
    release_settled = await _settle_task(
        asyncio.create_task(pool.release(connection)),
        cancel_on_cancellation=False,
    )

    cancellation = (
        operation_settled.cancellation or release_settled.cancellation
    )
    operation_failure = operation_settled.failure
    release_failure = release_settled.failure
    if cancellation is not None:
        cleanup_failure = release_failure or operation_failure
        if cleanup_failure is not None:
            raise cancellation from cleanup_failure
        raise cancellation
    if release_failure is not None:
        if operation_failure is not None:
            raise release_failure from operation_failure
        raise release_failure
    if operation_failure is not None:
        raise operation_failure
    assert operation_settled.result is not _NO_RESULT
    return cast("T", operation_settled.result)


async def install_postgres(pool: asyncpg.Pool) -> None:
    """Install the fixed ``dr_store`` schema into an empty namespace.

    The caller owns ``pool``. This operation acquires and releases one
    connection without closing the pool.
    """
    if not isinstance(pool, asyncpg.Pool):
        raise TypeError("pool must be an asyncpg.Pool")

    async def install(connection: asyncpg.Connection) -> None:
        requirements = await connection.fetchrow(_DATABASE_REQUIREMENTS_SQL)
        assert requirements is not None
        _validate_database(
            version_num=requirements["server_version_num"],
            server_encoding=requirements["server_encoding"],
        )
        await connection.execute(_INSTALL_SQL)
        await connection.execute(
            _INSERT_SCHEMA_FORMAT_SQL,
            _POSTGRES_SCHEMA_FORMAT,
        )

    await _run_pool_operation(pool, install, transactional=True)


class PostgresBackend:
    """Shared PostgreSQL storage through a caller-owned asynchronous pool."""

    _pool: asyncpg.Pool

    def __init__(self, pool: asyncpg.Pool) -> None:
        del pool
        raise TypeError("use 'await PostgresBackend.open(pool)'")

    @classmethod
    async def open(cls, pool: asyncpg.Pool) -> Self:
        """Validate the installed schema format and use ``pool``."""
        if not isinstance(pool, asyncpg.Pool):
            raise TypeError("pool must be an asyncpg.Pool")

        async def validate(connection: asyncpg.Connection) -> None:
            rows = await connection.fetch(_GET_SCHEMA_FORMAT_SQL)
            _validate_schema_format([row["format"] for row in rows])

        await _run_pool_operation(pool, validate, transactional=False)
        self = object.__new__(cls)
        self._pool = pool
        return self

    async def _execute(
        self,
        connection: asyncpg.Connection,
        query: str,
        *args: object,
    ) -> str:
        return await connection.execute(query, *args)

    async def _fetch(
        self,
        connection: asyncpg.Connection,
        query: str,
        *args: object,
    ) -> list[asyncpg.Record]:
        return await connection.fetch(query, *args)

    async def _fetchrow(
        self,
        connection: asyncpg.Connection,
        query: str,
        *args: object,
    ) -> asyncpg.Record | None:
        return await connection.fetchrow(query, *args)

    async def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        _validate_reference_schema(schema)
        _validate_content_hash(content_hash)

        async def put(connection: asyncpg.Connection) -> PutOutcome:
            inserted = await self._fetchrow(
                connection,
                _INSERT_OBJECT_SQL,
                content_hash,
                schema,
                canonical,
            )
            if inserted is not None:
                return PutOutcome(
                    inserted=True,
                    stored_schema=inserted["schema"],
                    stored_canonical=inserted["canonical"],
                )
            stored = await self._fetchrow(
                connection,
                _GET_EXACT_OBJECT_SQL,
                content_hash,
                schema,
            )
            assert stored is not None
            return PutOutcome(
                inserted=False,
                stored_schema=stored["schema"],
                stored_canonical=stored["canonical"],
            )

        return await _run_pool_operation(
            self._pool,
            put,
            transactional=True,
        )

    async def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        _validate_reference_schema(schema)
        _validate_content_hash(content_hash)

        async def get(
            connection: asyncpg.Connection,
        ) -> tuple[str, str] | None:
            row = await self._fetchrow(
                connection,
                _GET_OBJECT_SQL,
                content_hash,
                schema,
            )
            if row is None:
                return None
            return (row["schema"], row["canonical"])

        return await _run_pool_operation(
            self._pool,
            get,
            transactional=False,
        )

    async def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        _validate_binding_key(key)
        _validate_reference_schema(schema)
        _validate_content_hash(content_hash)

        async def bind(connection: asyncpg.Connection) -> BindOutcome:
            inserted = await self._fetchrow(
                connection,
                _INSERT_BINDING_SQL,
                key,
                schema,
                content_hash,
            )
            row = await self._fetchrow(
                connection,
                _GET_BINDING_SQL,
                key,
            )
            assert row is not None
            return BindOutcome(
                bound=inserted is not None,
                existing_schema=row["schema"],
                existing_content_hash=row["content_hash"],
            )

        return await _run_pool_operation(
            self._pool,
            bind,
            transactional=True,
        )

    async def get_binding(self, *, key: str) -> tuple[str, str] | None:
        _validate_binding_key(key)

        async def get(
            connection: asyncpg.Connection,
        ) -> tuple[str, str] | None:
            row = await self._fetchrow(connection, _GET_BINDING_SQL, key)
            if row is None:
                return None
            return (row["schema"], row["content_hash"])

        return await _run_pool_operation(
            self._pool,
            get,
            transactional=False,
        )

    async def get_bound_objects(
        self,
        *,
        keys: tuple[str, ...],
    ) -> dict[str, BoundObjectRow]:
        for key in keys:
            _validate_binding_key(key)
        distinct_keys = tuple(dict.fromkeys(keys))
        if not distinct_keys:
            return {}

        bindings: dict[str, tuple[str, str]] = {}
        objects: dict[tuple[str, str], str] = {}

        async def get(connection: asyncpg.Connection) -> None:
            for chunk in _chunks(distinct_keys):
                rows = await self._fetch(
                    connection,
                    _FETCH_BINDINGS_SQL,
                    list(chunk),
                )
                for row in rows:
                    bindings[row["key"]] = (
                        row["schema"],
                        row["content_hash"],
                    )

            references = tuple(dict.fromkeys(bindings.values()))
            for chunk in _chunks(references):
                rows = await self._fetch(
                    connection,
                    _FETCH_OBJECTS_SQL,
                    [reference[1] for reference in chunk],
                    [reference[0] for reference in chunk],
                )
                for row in rows:
                    objects[(row["schema"], row["content_hash"])] = row[
                        "canonical"
                    ]

        await _run_pool_operation(
            self._pool,
            get,
            transactional=False,
        )

        results: dict[str, BoundObjectRow] = {}
        for key, (schema, content_hash) in bindings.items():
            canonical = objects.get((schema, content_hash))
            results[key] = BoundObjectRow(
                binding_schema=schema,
                binding_content_hash=content_hash,
                object_schema=schema if canonical is not None else None,
                canonical=canonical,
            )
        return results

    async def put_bound_objects(
        self,
        *,
        entries: tuple[BoundObjectWrite, ...],
    ) -> dict[str, BindOutcome]:
        for entry in entries:
            _validate_binding_key(entry.key)
            _validate_reference_schema(entry.schema)
            _validate_content_hash(entry.content_hash)
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
        inserted_keys: set[str] = set()
        stored_objects: dict[tuple[str, str], str] = {}
        stored_bindings: dict[str, tuple[str, str]] = {}

        async def put(connection: asyncpg.Connection) -> None:
            for chunk in _chunks(references):
                await self._execute(
                    connection,
                    _INSERT_OBJECTS_SQL,
                    [reference[1] for reference in chunk],
                    [reference[0] for reference in chunk],
                    [objects[reference] for reference in chunk],
                )
            for chunk in _chunks(references):
                rows = await self._fetch(
                    connection,
                    _FETCH_OBJECTS_SQL,
                    [reference[1] for reference in chunk],
                    [reference[0] for reference in chunk],
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

            for chunk in _chunks(distinct_writes):
                rows = await self._fetch(
                    connection,
                    _INSERT_BINDINGS_SQL,
                    [entry.key for entry in chunk],
                    [entry.schema for entry in chunk],
                    [entry.content_hash for entry in chunk],
                )
                inserted_keys.update(row["key"] for row in rows)
            for chunk in _chunks(tuple(writes)):
                rows = await self._fetch(
                    connection,
                    _FETCH_BINDINGS_SQL,
                    list(chunk),
                )
                for row in rows:
                    stored_bindings[row["key"]] = (
                        row["schema"],
                        row["content_hash"],
                    )

        await _run_pool_operation(
            self._pool,
            put,
            transactional=True,
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
