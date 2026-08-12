from __future__ import annotations

import inspect
import os
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from dr_serialize import Jsonable
    from sqlalchemy.engine import Connection

from dr_store import (
    BindStatus,
    MemoryBackend,
    ObjectReference,
    ObjectStore,
    PostgresBackend,
    PutStatus,
    StoreHit,
    compute_content_hash,
)
from tests.conftest import _sync_dsn

SCHEMA = "example.record"
RECORD: Jsonable = {"value": "stored"}
CANONICAL = '{"value":"stored"}'
CONTENT_HASH = compute_content_hash(RECORD)
KEY = "evidence-key"
OTHER_KEY = "missing-key"


def _sync_engine():
    from sqlalchemy import create_engine

    dsn = os.environ.get("DR_STORE_POSTGRES_DSN")
    if dsn is None:
        pytest.skip("DR_STORE_POSTGRES_DSN is not configured")
    return create_engine(
        _sync_dsn(dsn),
        connect_args={"options": "-c search_path=pg_catalog"},
    )


def test_get_enlisted_requires_postgres_backend() -> None:
    store = ObjectStore(MemoryBackend())
    reference = ObjectReference(schema=SCHEMA, content_hash=CONTENT_HASH)
    with pytest.raises(TypeError, match="PostgresBackend"):
        store.get_enlisted(cast("Connection", object()), reference)


def test_resolve_enlisted_requires_postgres_backend() -> None:
    store = ObjectStore(MemoryBackend())
    with pytest.raises(TypeError, match="PostgresBackend"):
        store.resolve_enlisted(cast("Connection", object()), KEY)


def test_get_many_enlisted_requires_postgres_backend() -> None:
    store = ObjectStore(MemoryBackend())
    with pytest.raises(TypeError, match="PostgresBackend"):
        store.get_many_enlisted(
            cast("Connection", object()),
            [KEY],
            schema=SCHEMA,
        )


def test_put_enlisted_requires_postgres_backend() -> None:
    store = ObjectStore(MemoryBackend())
    with pytest.raises(TypeError, match="PostgresBackend"):
        store.put_enlisted(
            cast("Connection", object()), SCHEMA, {"value": "stored"}
        )


async def test_put_enlisted_stores_on_caller_connection(
    postgres_backend: PostgresBackend,
) -> None:
    store = ObjectStore(postgres_backend)
    engine = _sync_engine()
    try:
        with engine.connect() as connection, connection.begin():
            reference, status = store.put_enlisted(
                connection,
                SCHEMA,
                RECORD,
            )
            assert status is PutStatus.STORED
            assert reference.content_hash == CONTENT_HASH
            assert store.get_enlisted(connection, reference) == RECORD
            row = postgres_backend.get_object_enlisted(
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
                connection=connection,
            )
            assert row == (SCHEMA, CANONICAL)
    finally:
        engine.dispose()


async def test_resolve_enlisted_reads_binding_on_caller_connection(
    postgres_backend: PostgresBackend,
) -> None:
    store = ObjectStore(postgres_backend)
    reference = ObjectReference(schema=SCHEMA, content_hash=CONTENT_HASH)
    await postgres_backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    )
    engine = _sync_engine()
    try:
        with engine.connect() as connection, connection.begin():
            assert store.resolve_enlisted(connection, OTHER_KEY) is None
            store.bind_enlisted(connection, KEY, reference)
            assert store.resolve_enlisted(connection, KEY) == reference
    finally:
        engine.dispose()


async def test_get_many_enlisted_returns_verified_hits_and_unbound_none(
    postgres_backend: PostgresBackend,
) -> None:
    store = ObjectStore(postgres_backend)
    reference = ObjectReference(schema=SCHEMA, content_hash=CONTENT_HASH)
    await postgres_backend.put_object(
        schema=SCHEMA,
        content_hash=CONTENT_HASH,
        canonical=CANONICAL,
    )
    engine = _sync_engine()
    try:
        with engine.connect() as connection, connection.begin():
            store.bind_enlisted(connection, KEY, reference)
            assert store.get_many_enlisted(
                connection,
                [OTHER_KEY, KEY, KEY],
                schema=SCHEMA,
            ) == {
                OTHER_KEY: None,
                KEY: StoreHit(record=RECORD),
            }
    finally:
        engine.dispose()


async def test_bind_enlisted_is_idempotent(
    postgres_backend: PostgresBackend,
) -> None:
    store = ObjectStore(postgres_backend)
    reference = ObjectReference(schema=SCHEMA, content_hash=CONTENT_HASH)
    engine = _sync_engine()
    try:
        await postgres_backend.put_object(
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
            canonical=CANONICAL,
        )
        with engine.connect() as connection, connection.begin():
            assert (
                store.bind_enlisted(connection, KEY, reference)
                is BindStatus.BOUND
            )
            assert (
                store.bind_enlisted(connection, KEY, reference)
                is BindStatus.IDEMPOTENT
            )
    finally:
        engine.dispose()


def test_object_store_enlisted_methods_are_sync() -> None:
    for name in (
        "put_enlisted",
        "bind_enlisted",
        "put_many_enlisted",
        "get_bound_objects_enlisted",
        "get_enlisted",
        "get_many_enlisted",
        "resolve_enlisted",
    ):
        assert not inspect.iscoroutinefunction(getattr(ObjectStore, name))
