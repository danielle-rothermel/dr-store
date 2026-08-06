from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from dr_serialize import StrictJsonError

from dr_store import (
    ObjectReference,
    RecordCache,
    derive_cache_key,
)

if TYPE_CHECKING:
    from dr_serialize import Jsonable

    from dr_store import Backend, ObjectStore

KEY = "example.memo.v1:key"
SCHEMA = "example.record"
RECORD: Jsonable = {"payload": {"a": 1, "b": [2, 3]}}
OTHER: Jsonable = {"payload": "different"}


@pytest.fixture
def cache(store: ObjectStore) -> RecordCache:
    return RecordCache(store)


def test_unbound_key_is_a_miss(cache: RecordCache) -> None:
    assert cache.get(KEY, schema=SCHEMA) is None


def test_put_then_get_round_trips(cache: RecordCache) -> None:
    reference = cache.put(KEY, SCHEMA, RECORD)
    assert reference.schema == SCHEMA
    assert cache.get(KEY, schema=SCHEMA) == RECORD


def test_other_schema_is_a_miss(cache: RecordCache) -> None:
    cache.put(KEY, SCHEMA, RECORD)
    assert cache.get(KEY, schema="other.schema") is None


def test_missing_object_is_a_miss(
    backend: Backend,
    cache: RecordCache,
) -> None:
    reference = ObjectReference.for_record(SCHEMA, RECORD)
    backend.bind(
        key=KEY,
        schema=reference.schema,
        content_hash=reference.content_hash,
    )
    assert cache.get(KEY, schema=SCHEMA) is None


def test_corrupted_object_is_a_miss(
    backend: Backend,
    cache: RecordCache,
) -> None:
    # Store text that does not match the address it is stored under.
    reference = ObjectReference.for_record(SCHEMA, RECORD)
    backend.put_object(
        schema=reference.schema,
        content_hash=reference.content_hash,
        canonical='{"tampered":true}',
    )
    backend.bind(
        key=KEY,
        schema=reference.schema,
        content_hash=reference.content_hash,
    )
    assert cache.get(KEY, schema=SCHEMA) is None


def test_conflicting_put_returns_the_first_winner(
    cache: RecordCache,
) -> None:
    first = cache.put(KEY, SCHEMA, RECORD)
    second = cache.put(KEY, SCHEMA, OTHER)
    assert second == first
    assert cache.get(KEY, schema=SCHEMA) == RECORD


def test_invalid_record_raises(cache: RecordCache) -> None:
    with pytest.raises(StrictJsonError):
        cache.put(
            KEY,
            SCHEMA,
            {"value": float("nan")},
        )


def test_derived_key_is_deterministic_and_canonical() -> None:
    assert derive_cache_key("ns.v1", {"a": 1, "b": 2}) == derive_cache_key(
        "ns.v1", {"b": 2, "a": 1}
    )
    assert derive_cache_key("ns.v1", RECORD) != derive_cache_key(
        "ns.v2", RECORD
    )
    assert derive_cache_key("ns.v1", RECORD) != derive_cache_key(
        "ns.v1", OTHER
    )
