from __future__ import annotations

import asyncio
import inspect
import os
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from sqlalchemy.engine import Connection, Engine

from dr_store import (
    BindOutcome,
    BoundObjectWrite,
    ObjectConflictError,
    PostgresBackend,
    install_postgres,
)
from dr_store.storage_backends import postgresql
from tests.conftest import _async_dsn, _sync_dsn

SCHEMA = "example.record"
CONTENT_HASH = "a" * 64
CANONICAL = '{"value":"stored"}'
WATCHDOG_SECONDS = 15


def _postgres_sync_engine() -> Engine:
    dsn = os.environ.get("DR_STORE_POSTGRES_DSN")
    if dsn is None:
        pytest.skip("DR_STORE_POSTGRES_DSN is not configured")
    return create_engine(
        _sync_dsn(dsn),
        connect_args={"options": "-c search_path=pg_catalog"},
    )


@pytest.fixture
async def postgres_backend(
    postgres_engine: AsyncEngine,
) -> PostgresBackend:
    await install_postgres(postgres_engine)
    return await PostgresBackend.open(postgres_engine)


def test_direct_construction_is_not_public() -> None:
    with pytest.raises(
        TypeError, match=r"use 'await PostgresBackend\.open\(engine\)'"
    ):
        PostgresBackend(cast("AsyncEngine", object()))


async def test_open_rejects_non_engine_without_connection_work() -> None:
    with pytest.raises(
        TypeError,
        match=r"engine must be a sqlalchemy\.ext\.asyncio\.AsyncEngine",
    ):
        await PostgresBackend.open(cast("AsyncEngine", object()))


async def test_direct_construction_rejects_a_real_engine(
    postgres_engine: AsyncEngine,
) -> None:
    with pytest.raises(
        TypeError, match=r"use 'await PostgresBackend\.open\(engine\)'"
    ):
        PostgresBackend(postgres_engine)


async def test_open_rejects_nonpositive_batch_chunk_size(
    postgres_engine: AsyncEngine,
) -> None:
    await install_postgres(postgres_engine)
    with pytest.raises(ValueError, match="batch_chunk_size must be positive"):
        await PostgresBackend.open(postgres_engine, batch_chunk_size=0)
    with pytest.raises(ValueError, match="batch_chunk_size must be positive"):
        await PostgresBackend.open(postgres_engine, batch_chunk_size=-1)


async def test_missing_namespace_fails_without_implicit_installation(
    postgres_engine: AsyncEngine,
) -> None:
    with pytest.raises(DBAPIError):
        await PostgresBackend.open(postgres_engine)

    async with postgres_engine.connect() as connection:
        present = await connection.scalar(
            text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM pg_catalog.pg_namespace
                    WHERE pg_namespace.nspname = 'dr_store'
                )
                """
            )
        )
        assert present is False


@pytest.mark.parametrize(
    "formats",
    [
        [],
        [None],
        [1],
        ["dr-store-postgresql-v2"],
        ["dr-store-postgresql-v1"] * 2,
    ],
)
def test_schema_format_validation_requires_one_exact_marker(
    formats: list[object],
) -> None:
    with pytest.raises(
        RuntimeError,
        match="schema format marker is missing, malformed, or unsupported",
    ):
        postgresql._validate_schema_format(formats)


@pytest.mark.parametrize(
    ("mutate_sql", "stored_format"),
    [
        ("DELETE FROM dr_store.schema_format", None),
        (
            "UPDATE dr_store.schema_format "
            "SET format = 'dr-store-postgresql-v2'",
            "dr-store-postgresql-v2",
        ),
    ],
)
async def test_open_rejects_incompatible_marker_without_modifying_storage(
    postgres_engine: AsyncEngine,
    mutate_sql: str,
    stored_format: str | None,
) -> None:
    await install_postgres(postgres_engine)
    async with postgres_engine.begin() as connection:
        await connection.execute(text(mutate_sql))

    with pytest.raises(
        RuntimeError,
        match="schema format marker is missing, malformed, or unsupported",
    ):
        await PostgresBackend.open(postgres_engine)

    async with postgres_engine.connect() as connection:
        formats = [
            row["format"]
            for row in (
                await connection.execute(
                    text("SELECT format FROM dr_store.schema_format")
                )
            ).mappings()
        ]
        assert formats == ([] if stored_format is None else [stored_format])
        assert (
            await connection.scalar(
                text("SELECT pg_catalog.count(*) FROM dr_store.objects")
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT pg_catalog.count(*) FROM dr_store.bindings")
            )
            == 0
        )


async def test_backend_uses_qualified_tables_under_nondefault_search_path(
    postgres_engine: AsyncEngine,
    postgres_backend: PostgresBackend,
) -> None:
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SHOW search_path")) == "pg_catalog"
        )

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


async def test_backend_does_not_stringify_retain_or_dispose_engine(
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await install_postgres(postgres_engine)

    def fail_stringification(_engine: AsyncEngine) -> str:
        pytest.fail("backend stringified the caller-owned engine")

    monkeypatch.setattr(AsyncEngine, "__str__", fail_stringification)
    backend = await PostgresBackend.open(postgres_engine)
    assert backend.__dict__ == {
        "_engine": postgres_engine,
        "_batch_chunk_size": 512,
    }
    assert await backend.get_binding(key="missing") is None
    async with postgres_engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1


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
        key: (row.binding_schema, row.canonical) for key, row in rows.items()
    } == {value: (value, f'{{"schema":"{value}"}}') for value in values}


async def test_committed_rows_are_visible_through_independent_engines(
    postgres_backend: PostgresBackend,
) -> None:
    independent_engine = create_async_engine(
        _async_dsn(os.environ["DR_STORE_POSTGRES_DSN"]),
        pool_size=2,
        max_overflow=0,
        connect_args={"options": "-c search_path=public"},
    )
    try:
        second = await PostgresBackend.open(independent_engine)
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
        await independent_engine.dispose()


async def _wait_until_insert_is_lock_blocked(
    connection: AsyncConnection,
) -> None:
    async def observe() -> None:
        while not await connection.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_locks AS locks
                    WHERE locks.relation = 'dr_store.objects'::regclass
                        AND NOT locks.granted
                )
                """
            )
        ):
            continue

    await asyncio.wait_for(observe(), WATCHDOG_SECONDS)


async def test_cancelled_open_releases_connection_for_engine_reuse(
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await install_postgres(postgres_engine)
    started = asyncio.Event()
    release = asyncio.Event()
    gated_calls = 0
    original_run = postgresql._run_connection_operation

    async def gated_run[T](
        engine: AsyncEngine,
        operation: Callable[[AsyncConnection], Awaitable[T]],
        *,
        write: bool,
    ) -> T:
        nonlocal gated_calls
        if not write:
            gated_calls += 1
            if gated_calls == 1:
                started.set()
                await release.wait()
        return await original_run(engine, operation, write=write)

    monkeypatch.setattr(postgresql, "_run_connection_operation", gated_run)
    operation = asyncio.create_task(PostgresBackend.open(postgres_engine))
    await started.wait()
    operation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert gated_calls == 1
    backend = await PostgresBackend.open(postgres_engine)
    assert await backend.get_binding(key="missing") is None


async def test_cancelled_locked_write_releases_connection_for_engine_reuse(
    postgres_engine: AsyncEngine,
    postgres_backend: PostgresBackend,
) -> None:
    async with postgres_engine.connect() as blocker:
        await blocker.execute(text("BEGIN"))
        await blocker.execute(
            text("LOCK TABLE dr_store.objects IN ACCESS EXCLUSIVE MODE")
        )
        operation = asyncio.create_task(
            postgres_backend.put_object(
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
                canonical=CANONICAL,
            )
        )
        async with postgres_engine.connect() as observer:
            await _wait_until_insert_is_lock_blocked(observer)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, WATCHDOG_SECONDS)
        await blocker.execute(text("ROLLBACK"))

    assert (
        await postgres_backend.put_object(
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
            canonical=CANONICAL,
        )
    ).inserted


async def test_enlisted_write_is_not_visible_before_caller_commits(
    postgres_backend: PostgresBackend,
) -> None:
    engine = _postgres_sync_engine()
    try:
        with engine.connect() as connection, connection.begin():
            outcome = postgres_backend.put_object_enlisted(
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
                canonical=CANONICAL,
                connection=connection,
            )
            assert outcome.inserted
            assert (
                await postgres_backend.get_object(
                    schema=SCHEMA,
                    content_hash=CONTENT_HASH,
                )
                is None
            )
            assert postgres_backend.get_object_enlisted(
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
                connection=connection,
            ) == (SCHEMA, CANONICAL)
            row = connection.scalar(
                text(
                    """
                    SELECT canonical
                    FROM dr_store.objects
                    WHERE content_hash = :content_hash
                        AND schema = :schema
                    """
                ),
                {
                    "content_hash": CONTENT_HASH,
                    "schema": SCHEMA,
                },
            )
            assert row == CANONICAL
    finally:
        engine.dispose()

    assert await postgres_backend.get_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
    ) == (SCHEMA, CANONICAL)


def test_enlisted_write_does_not_commit_or_close_caller_connection(
    postgres_backend: PostgresBackend,
) -> None:
    engine = _postgres_sync_engine()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                postgres_backend.bind_enlisted(
                    key="enlisted",
                    schema=SCHEMA,
                    content_hash=CONTENT_HASH,
                    connection=connection,
                )
                assert postgres_backend.get_binding_enlisted(
                    key="enlisted",
                    connection=connection,
                ) == (SCHEMA, CONTENT_HASH)
                transaction.rollback()
            finally:
                assert not connection.closed
    finally:
        engine.dispose()


async def test_enlisted_write_is_invisible_after_rollback(
    postgres_backend: PostgresBackend,
) -> None:
    engine = _postgres_sync_engine()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            postgres_backend.bind_enlisted(
                key="enlisted",
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
                connection=connection,
            )
            transaction.rollback()
    finally:
        engine.dispose()

    assert await postgres_backend.get_binding(key="enlisted") is None


def test_enlisted_batch_read_sees_snapshot_before_caller_commits(
    postgres_backend: PostgresBackend,
) -> None:
    entry = BoundObjectWrite(
        key="batch-enlisted",
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    )
    engine = _postgres_sync_engine()
    try:
        with engine.connect() as connection, connection.begin():
            postgres_backend.put_bound_objects_enlisted(
                entries=(entry,),
                connection=connection,
            )
            assert (
                postgres_backend.get_bound_objects_enlisted(
                    keys=("batch-enlisted",),
                    connection=connection,
                )["batch-enlisted"].canonical
                == CANONICAL
            )
    finally:
        engine.dispose()


def test_enlisted_batch_conflict_retains_object_inserts(
    postgres_backend: PostgresBackend,
) -> None:
    other_hash = "b" * 64
    engine = _postgres_sync_engine()
    try:
        with engine.connect() as connection:
            postgres_backend.put_object_enlisted(
                schema=SCHEMA,
                content_hash=other_hash,
                canonical='{"value":"other"}',
                connection=connection,
            )
            connection.commit()
            transaction = connection.begin()
            try:
                with pytest.raises(ObjectConflictError):
                    postgres_backend.put_bound_objects_enlisted(
                        entries=(
                            BoundObjectWrite(
                                "first-key",
                                SCHEMA,
                                CONTENT_HASH,
                                CANONICAL,
                            ),
                            BoundObjectWrite(
                                "second-key",
                                SCHEMA,
                                other_hash,
                                '{"value":"tampered"}',
                            ),
                        ),
                        connection=connection,
                    )
                row = connection.scalar(
                    text(
                        """
                        SELECT canonical
                        FROM dr_store.objects
                        WHERE content_hash = :content_hash AND schema = :schema
                        """
                    ),
                    {"content_hash": CONTENT_HASH, "schema": SCHEMA},
                )
                assert row == CANONICAL
                assert (
                    postgres_backend.get_binding_enlisted(
                        key="first-key",
                        connection=connection,
                    )
                    is None
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_enlisted_read_does_not_close_caller_connection(
    postgres_backend: PostgresBackend,
) -> None:
    engine = _postgres_sync_engine()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                postgres_backend.put_object_enlisted(
                    schema=SCHEMA,
                    content_hash=CONTENT_HASH,
                    canonical=CANONICAL,
                    connection=connection,
                )
                assert postgres_backend.get_object_enlisted(
                    schema=SCHEMA,
                    content_hash=CONTENT_HASH,
                    connection=connection,
                ) == (SCHEMA, CANONICAL)
                transaction.rollback()
            finally:
                assert not connection.closed
    finally:
        engine.dispose()


def test_async_methods_do_not_accept_connection_kwarg(
    postgres_backend: PostgresBackend,
) -> None:
    for name in (
        "put_object",
        "get_object",
        "bind",
        "get_binding",
        "get_bound_objects",
        "put_bound_objects",
    ):
        assert (
            "connection"
            not in inspect.signature(
                getattr(postgres_backend, name)
            ).parameters
        )


async def test_batch_statements_scale_with_chunks_and_distinct_objects(
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await install_postgres(postgres_engine)
    backend = await PostgresBackend.open(
        postgres_engine,
        batch_chunk_size=512,
    )
    statements: list[str] = []
    fetched_object_rows: list[int] = []
    execute_sync = postgresql._execute_sync
    fetch_sync = postgresql._fetch_sync

    def counted_execute_sync(
        connection: Connection,
        query: str,
        parameters: dict[str, object] | None = None,
    ) -> None:
        statements.append(query)
        execute_sync(connection, query, parameters)

    def counted_fetch_sync(
        connection: Connection,
        query: str,
        parameters: dict[str, object] | None = None,
    ) -> list[Mapping[str, object]]:
        statements.append(query)
        rows = fetch_sync(connection, query, parameters)
        if query == postgresql._FETCH_OBJECTS_SQL:
            fetched_object_rows.append(len(rows))
        return rows

    monkeypatch.setattr(postgresql, "_execute_sync", counted_execute_sync)
    monkeypatch.setattr(postgresql, "_fetch_sync", counted_fetch_sync)

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
    assert len(await backend.put_bound_objects(entries=small)) == 3
    assert len(statements) == 4

    statements.clear()
    fetched_object_rows.clear()
    assert (
        len(
            await backend.get_bound_objects(
                keys=tuple(entry.key for entry in small)
            )
        )
        == 3
    )
    assert len(statements) == 2
    assert fetched_object_rows == [2]

    statements.clear()
    count = 513
    many = tuple(
        BoundObjectWrite(
            key=f"many-{index}",
            schema=SCHEMA,
            content_hash=f"{index + 2:064x}",
            canonical=f'{{"value":{index}}}',
        )
        for index in range(count)
    )
    outcomes = await backend.put_bound_objects(entries=many)
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
