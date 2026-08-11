from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dr_store import (
    ContentHashMismatchError,
    ObjectNotFoundError,
    ObjectReference,
    ObjectStore,
    RecordCache,
    SchemaMismatchError,
    StoreHit,
    compute_content_hash,
)

if TYPE_CHECKING:
    from dr_serialize import Jsonable

    from tests.object_store.conftest import ControlledBackend

SCHEMA = "example.record"
OTHER_SCHEMA = "other.record"
RECORD_A: Jsonable = {"payload": {"id": "a"}}
RECORD_B: Jsonable = {"payload": {"id": "b"}}
KEY_A = "key:a"
KEY_B = "key:b"
KEY_MISSING = "key:missing"


async def test_put_many_stores_and_returns_binding_winners(
    store: ObjectStore,
) -> None:
    entries = {
        KEY_A: (SCHEMA, RECORD_A),
        KEY_B: (SCHEMA, RECORD_B),
    }
    references = await store.put_many(entries)
    assert set(references) == {KEY_A, KEY_B}
    assert references[KEY_A].content_hash == compute_content_hash(RECORD_A)
    assert references[KEY_B].content_hash == compute_content_hash(RECORD_B)


async def test_put_many_replays_idempotently(store: ObjectStore) -> None:
    entries = {KEY_A: (SCHEMA, RECORD_A)}
    first = await store.put_many(entries)
    second = await store.put_many(entries)
    assert second == first


async def test_get_many_returns_verified_records_and_unbound_none(
    store: ObjectStore,
) -> None:
    await store.put_many(
        {
            KEY_A: (SCHEMA, RECORD_A),
            KEY_B: (SCHEMA, RECORD_B),
        }
    )
    assert await store.get_many(
        [KEY_MISSING, KEY_A, KEY_B, KEY_A],
        schema=SCHEMA,
    ) == {
        KEY_MISSING: None,
        KEY_A: StoreHit(record=RECORD_A),
        KEY_B: StoreHit(record=RECORD_B),
    }


async def test_get_many_distinguishes_unbound_from_null_record(
    store: ObjectStore,
) -> None:
    await store.put_many({KEY_A: (SCHEMA, None)})
    assert await store.get_many([KEY_A, KEY_MISSING], schema=SCHEMA) == {
        KEY_A: StoreHit(record=None),
        KEY_MISSING: None,
    }


async def test_get_many_raises_on_binding_schema_mismatch(
    store: ObjectStore,
) -> None:
    reference, _ = await store.put(SCHEMA, RECORD_A)
    await store.bind(KEY_A, reference)
    with pytest.raises(SchemaMismatchError):
        await store.get_many([KEY_A], schema=OTHER_SCHEMA)


async def test_get_many_raises_on_unverifiable_stored_content(
    controlled_backend: ControlledBackend,
) -> None:
    store = ObjectStore(controlled_backend)
    reference, _ = await store.put(SCHEMA, RECORD_A)
    await store.bind(KEY_A, reference)
    controlled_backend.object_rows[
        (reference.schema, reference.content_hash)
    ] = "not valid json"
    with pytest.raises(ContentHashMismatchError):
        await store.get_many([KEY_A], schema=SCHEMA)


async def test_get_many_raises_when_referenced_object_is_missing(
    controlled_backend: ControlledBackend,
) -> None:
    reference = ObjectReference.for_record(SCHEMA, RECORD_A)
    controlled_backend.bindings[KEY_A] = (
        reference.schema,
        reference.content_hash,
    )
    store = ObjectStore(controlled_backend)
    with pytest.raises(ObjectNotFoundError):
        await store.get_many([KEY_A], schema=SCHEMA)


async def test_record_cache_still_reports_corruption_as_miss(
    controlled_backend: ControlledBackend,
) -> None:
    store = ObjectStore(controlled_backend)
    reference, _ = await store.put(SCHEMA, RECORD_A)
    await store.bind(KEY_A, reference)
    controlled_backend.object_rows[
        (reference.schema, reference.content_hash)
    ] = "not valid json"
    cache = RecordCache(store)
    assert await cache.get_many([KEY_A], schema=SCHEMA) == {KEY_A: None}
