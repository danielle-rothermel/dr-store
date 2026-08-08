from __future__ import annotations

import asyncio
import os
from typing import cast

import asyncpg
import pytest

from dr_store import (
    BindOutcome,
    BoundObjectWrite,
    PostgresBackend,
    install_postgres,
)
from dr_store.storage_backends import postgresql

SCHEMA = "example.record"
CONTENT_HASH = "a" * 64
CANONICAL = '{"value":"stored"}'
WATCHDOG_SECONDS = 15


@pytest.fixture
async def postgres_backend(
    postgres_pool: asyncpg.Pool,
) -> PostgresBackend:
    await install_postgres(postgres_pool)
    return PostgresBackend(postgres_pool)


def test_rejects_non_pool_at_construction() -> None:
    with pytest.raises(TypeError, match=r"pool must be an asyncpg\.Pool"):
        PostgresBackend(cast("asyncpg.Pool", object()))


async def test_missing_namespace_fails_without_implicit_installation(
    postgres_pool: asyncpg.Pool,
) -> None:
    backend = PostgresBackend(postgres_pool)
    with pytest.raises(asyncpg.UndefinedTableError):
        await backend.get_binding(key="missing")


async def test_backend_uses_qualified_tables_under_nondefault_search_path(
    postgres_pool: asyncpg.Pool,
    postgres_backend: PostgresBackend,
) -> None:
    async with postgres_pool.acquire() as connection:
        assert await connection.fetchval("SHOW search_path") == "pg_catalog"

    outcome = await postgres_backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    )
    assert outcome.inserted
    assert await postgres_backend.get_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == (SCHEMA, CANONICAL)


async def test_backend_does_not_stringify_retain_or_close_pool(
    postgres_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await install_postgres(postgres_pool)

    def fail_stringification(_pool: asyncpg.Pool) -> str:
        pytest.fail("backend stringified the caller-owned pool")

    monkeypatch.setattr(asyncpg.Pool, "__str__", fail_stringification)
    backend = PostgresBackend(postgres_pool)
    assert backend.__dict__ == {"_pool": postgres_pool}
    assert await backend.get_binding(key="missing") is None
    assert not postgres_pool.is_closing()
    async with postgres_pool.acquire() as connection:
        assert await connection.fetchval("SELECT 1") == 1


async def test_exact_text_identity_through_backend(
    postgres_backend: PostgresBackend,
) -> None:
    composed = "\N{LATIN SMALL LETTER E WITH ACUTE}"
    decomposed = "e\N{COMBINING ACUTE ACCENT}"
    values = ("Case", "case", composed, decomposed)
    entries = tuple(
        BoundObjectWrite(
            key=value,
            schema=value,
            content_hash=f"{index:x}" * 64,
            canonical=f'{{"schema":"{value}"}}',
        )
        for index, value in enumerate(values)
    )
    outcomes = await postgres_backend.put_bound_objects(entries=entries)
    assert set(outcomes) == set(values)
    assert all(outcome.bound for outcome in outcomes.values())

    rows = await postgres_backend.get_bound_objects(keys=values)
    assert {
        key: (row.binding_schema, row.object_schema, row.canonical)
        for key, row in rows.items()
    } == {value: (value, value, f'{{"schema":"{value}"}}') for value in values}


async def test_committed_rows_are_visible_through_independent_pools(
    postgres_backend: PostgresBackend,
) -> None:
    connection_string = os.environ["DR_STORE_POSTGRES_DSN"]
    independent_pool = await asyncpg.create_pool(
        connection_string,
        min_size=1,
        max_size=2,
        server_settings={"search_path": "public"},
    )
    try:
        second = PostgresBackend(independent_pool)
        assert (
            await postgres_backend.bind(
                key="shared",
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
            )
        ).bound
        assert await second.get_binding(key="shared") == (
            SCHEMA,
            CONTENT_HASH,
        )
        assert (
            await second.bind(
                key="second",
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
            )
        ).bound
        assert await postgres_backend.get_binding(key="second") == (
            SCHEMA,
            CONTENT_HASH,
        )
    finally:
        await independent_pool.close()


async def _wait_until_insert_is_lock_blocked(
    connection: asyncpg.Connection,
) -> None:
    async def observe() -> None:
        while not await connection.fetchval(
            """
            SELECT pg_catalog.bool_or(
                activity.wait_event_type = 'Lock'
            )
            FROM pg_catalog.pg_stat_activity AS activity
            WHERE activity.datname = pg_catalog.current_database()
                AND activity.query LIKE
                    '%INSERT INTO dr_store.objects%'
                AND activity.pid <> pg_catalog.pg_backend_pid()
            """
        ):
            continue

    await asyncio.wait_for(observe(), WATCHDOG_SECONDS)


async def test_cancelled_locked_write_releases_connection_for_pool_reuse(
    postgres_pool: asyncpg.Pool,
    postgres_backend: PostgresBackend,
) -> None:
    async with postgres_pool.acquire() as blocker:
        transaction = blocker.transaction()
        await transaction.start()
        await blocker.execute(
            "LOCK TABLE dr_store.objects IN ACCESS EXCLUSIVE MODE"
        )
        operation = asyncio.create_task(
            postgres_backend.put_object(
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
                canonical=CANONICAL,
            )
        )
        async with postgres_pool.acquire() as observer:
            await _wait_until_insert_is_lock_blocked(observer)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, WATCHDOG_SECONDS)
        await transaction.rollback()

    assert postgres_pool.get_idle_size() == postgres_pool.get_size()
    assert (
        await postgres_backend.put_object(
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
            canonical=CANONICAL,
        )
    ).inserted


async def test_cancellation_waits_for_connection_release_before_terminating(
    postgres_pool: asyncpg.Pool,
) -> None:
    await install_postgres(postgres_pool)
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    gate_enabled = False

    async def gated_reset(_connection: asyncpg.Connection) -> None:
        if gate_enabled:
            release_started.set()
            await allow_release.wait()

    pool = await asyncpg.create_pool(
        os.environ["DR_STORE_POSTGRES_DSN"],
        min_size=1,
        max_size=1,
        reset=gated_reset,
        server_settings={"search_path": "pg_catalog"},
    )
    try:
        backend = PostgresBackend(pool)
        gate_enabled = True
        operation = asyncio.create_task(backend.get_binding(key="missing"))
        await asyncio.wait_for(release_started.wait(), WATCHDOG_SECONDS)

        operation.cancel()
        cancellation_observed = asyncio.Event()
        asyncio.get_running_loop().call_soon(cancellation_observed.set)
        await cancellation_observed.wait()
        assert not operation.done()

        operation.cancel()
        repeated_cancellation_observed = asyncio.Event()
        asyncio.get_running_loop().call_soon(
            repeated_cancellation_observed.set
        )
        await repeated_cancellation_observed.wait()
        assert not operation.done()

        allow_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, WATCHDOG_SECONDS)
        assert pool.get_idle_size() == 1
        assert await backend.get_binding(key="missing") is None
    finally:
        allow_release.set()
        await pool.close()


async def test_release_failure_is_cause_of_postgresql_cancellation(
    postgres_pool: asyncpg.Pool,
) -> None:
    await install_postgres(postgres_pool)
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    reset_failure = RuntimeError("injected reset failure")
    gate_enabled = False

    async def failing_reset(_connection: asyncpg.Connection) -> None:
        if gate_enabled:
            release_started.set()
            await allow_release.wait()
            raise reset_failure

    pool = await asyncpg.create_pool(
        os.environ["DR_STORE_POSTGRES_DSN"],
        min_size=1,
        max_size=1,
        reset=failing_reset,
        server_settings={"search_path": "pg_catalog"},
    )
    try:
        backend = PostgresBackend(pool)
        gate_enabled = True
        operation = asyncio.create_task(backend.get_binding(key="missing"))
        await asyncio.wait_for(release_started.wait(), WATCHDOG_SECONDS)
        operation.cancel()
        cancellation_observed = asyncio.Event()
        asyncio.get_running_loop().call_soon(cancellation_observed.set)
        await cancellation_observed.wait()
        assert not operation.done()

        allow_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(operation, WATCHDOG_SECONDS)
        assert caught.value.__cause__ is reset_failure

        gate_enabled = False
        assert await backend.get_binding(key="missing") is None
    finally:
        gate_enabled = False
        allow_release.set()
        await pool.close()


async def test_batch_statements_scale_with_chunks_and_distinct_objects(
    postgres_backend: PostgresBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    fetched_object_rows: list[int] = []
    execute = postgres_backend._execute
    fetch = postgres_backend._fetch

    async def counted_execute(
        connection: asyncpg.Connection,
        query: str,
        *args: object,
    ) -> str:
        statements.append(query)
        return await execute(connection, query, *args)

    async def counted_fetch(
        connection: asyncpg.Connection,
        query: str,
        *args: object,
    ) -> list[asyncpg.Record]:
        statements.append(query)
        rows = await fetch(connection, query, *args)
        if query == postgresql._FETCH_OBJECTS_SQL:
            fetched_object_rows.append(len(rows))
        return rows

    monkeypatch.setattr(postgres_backend, "_execute", counted_execute)
    monkeypatch.setattr(postgres_backend, "_fetch", counted_fetch)

    repeated = BoundObjectWrite(
        key="repeated-0",
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    )
    small = (
        repeated,
        BoundObjectWrite(
            key="repeated-1",
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
            canonical=CANONICAL,
        ),
        BoundObjectWrite(
            key="other",
            schema=SCHEMA,
            content_hash="b" * 64,
            canonical='{"value":"other"}',
        ),
    )
    assert len(await postgres_backend.put_bound_objects(entries=small)) == 3
    assert len(statements) == 4

    statements.clear()
    fetched_object_rows.clear()
    assert (
        len(
            await postgres_backend.get_bound_objects(
                keys=tuple(entry.key for entry in small)
            )
        )
        == 3
    )
    assert len(statements) == 2
    assert fetched_object_rows == [2]

    statements.clear()
    count = postgresql._BATCH_CHUNK_SIZE + 1
    many = tuple(
        BoundObjectWrite(
            key=f"many-{index}",
            schema=SCHEMA,
            content_hash=f"{index + 2:064x}",
            canonical=f'{{"value":{index}}}',
        )
        for index in range(count)
    )
    outcomes = await postgres_backend.put_bound_objects(entries=many)
    assert len(outcomes) == count
    assert len(statements) == 8


async def test_repeated_keys_are_deduplicated_before_sql(
    postgres_backend: PostgresBackend,
) -> None:
    outcomes = await postgres_backend.put_bound_objects(
        entries=(
            BoundObjectWrite("same", SCHEMA, CONTENT_HASH, CANONICAL),
            BoundObjectWrite(
                "same",
                "other.schema",
                "b" * 64,
                '{"value":"other"}',
            ),
        )
    )
    assert outcomes == {
        "same": BindOutcome(
            bound=True,
            existing_schema=SCHEMA,
            existing_content_hash=CONTENT_HASH,
        ),
    }
    assert await postgres_backend.get_binding(key="same") == (
        SCHEMA,
        CONTENT_HASH,
    )
