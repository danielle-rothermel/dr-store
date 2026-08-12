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
    compute_content_hash,
)
from tests.conftest import _sync_dsn

SCHEMA = "example.record"
RECORD: Jsonable = {"value": "stored"}
CANONICAL = '{"value":"stored"}'
CONTENT_HASH = compute_content_hash(RECORD)


def test_put_enlisted_requires_postgres_backend() -> None:
    store = ObjectStore(MemoryBackend())
    with pytest.raises(TypeError, match="PostgresBackend"):
        store.put_enlisted(
            cast("Connection", object()), SCHEMA, {"value": "stored"}
        )


async def test_put_enlisted_stores_on_caller_connection(
    postgres_backend: PostgresBackend,
) -> None:
    from sqlalchemy import create_engine

    dsn = os.environ.get("DR_STORE_POSTGRES_DSN")
    if dsn is None:
        pytest.skip("DR_STORE_POSTGRES_DSN is not configured")

    store = ObjectStore(postgres_backend)
    engine = create_engine(
        _sync_dsn(dsn),
        connect_args={"options": "-c search_path=pg_catalog"},
    )
    try:
        with engine.connect() as connection, connection.begin():
            reference, status = store.put_enlisted(
                connection,
                SCHEMA,
                RECORD,
            )
            assert status is PutStatus.STORED
            assert reference.content_hash == CONTENT_HASH
            row = postgres_backend.get_object_enlisted(
                schema=SCHEMA,
                content_hash=CONTENT_HASH,
                connection=connection,
            )
            assert row == (SCHEMA, CANONICAL)
    finally:
        engine.dispose()


async def test_bind_enlisted_is_idempotent(
    postgres_backend: PostgresBackend,
) -> None:
    from sqlalchemy import create_engine

    dsn = os.environ.get("DR_STORE_POSTGRES_DSN")
    if dsn is None:
        pytest.skip("DR_STORE_POSTGRES_DSN is not configured")

    store = ObjectStore(postgres_backend)
    reference = ObjectReference(schema=SCHEMA, content_hash=CONTENT_HASH)
    engine = create_engine(
        _sync_dsn(dsn),
        connect_args={"options": "-c search_path=pg_catalog"},
    )
    try:
        await postgres_backend.put_object(
            schema=SCHEMA,
            content_hash=CONTENT_HASH,
            canonical=CANONICAL,
        )
        with engine.connect() as connection, connection.begin():
            assert (
                store.bind_enlisted(connection, "evidence-key", reference)
                is BindStatus.BOUND
            )
            assert (
                store.bind_enlisted(connection, "evidence-key", reference)
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
    ):
        assert not inspect.iscoroutinefunction(getattr(ObjectStore, name))
