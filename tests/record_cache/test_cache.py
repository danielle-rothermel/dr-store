from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from dr_serialize import StrictJsonError

from dr_store import (
    CacheEntry,
    CacheHit,
    MemoryBackend,
    ObjectReference,
    ObjectStore,
    RecordCache,
    ReferenceValidationError,
    StoreError,
    compute_content_hash,
    derive_cache_key,
)
from dr_store import content_addressing as content_addressing_module
from dr_store import object_store as object_store_module

if TYPE_CHECKING:
    from dr_serialize import Jsonable

    from dr_store import Backend, BoundObjectRow

KEY = "example.memo.v1:key"
SCHEMA = "example.record"
RECORD: Jsonable = {"payload": {"a": 1, "b": [2, 3]}}
OTHER: Jsonable = {"payload": "different"}


def test_derived_key_uses_canonical_content_hash() -> None:
    assert derive_cache_key("ns.v1", RECORD) == (
        f"ns.v1:{compute_content_hash(RECORD)}"
    )


@pytest.mark.parametrize(
    "invalid_namespace",
    [None, 1, True],
)
def test_derived_key_rejects_non_string_namespace(
    invalid_namespace: object,
) -> None:
    with pytest.raises(TypeError):
        derive_cache_key(
            invalid_namespace,  # ty: ignore[invalid-argument-type]
            RECORD,
        )


@pytest.fixture
def cache(store: ObjectStore) -> RecordCache:
    return RecordCache(store)


def test_unbound_key_is_a_miss(cache: RecordCache) -> None:
    assert cache.get(KEY, schema=SCHEMA) is None


def test_put_then_get_round_trips(cache: RecordCache) -> None:
    reference = cache.put(KEY, SCHEMA, RECORD)
    assert reference.schema == SCHEMA
    assert cache.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)


def test_null_record_is_distinct_from_a_miss(cache: RecordCache) -> None:
    cache.put(KEY, SCHEMA, None)

    assert cache.get(KEY, schema=SCHEMA) == CacheHit(record=None)


def test_get_many_reports_each_hit_miss_null_and_corruption(
    backend: Backend,
    cache: RecordCache,
) -> None:
    hit_key = "batch:hit"
    null_key = "batch:null"
    missing_key = "batch:missing"
    corrupt_key = "batch:corrupt"
    cache.put_many(
        {
            hit_key: CacheEntry(schema=SCHEMA, record=RECORD),
            null_key: CacheEntry(schema=SCHEMA, record=None),
        }
    )
    corrupt_reference = ObjectReference.for_record(SCHEMA, OTHER)
    backend.put_object(
        schema=SCHEMA,
        content_hash=corrupt_reference.content_hash,
        canonical='{"tampered":true}',
    )
    backend.bind(
        key=corrupt_key,
        schema=SCHEMA,
        content_hash=corrupt_reference.content_hash,
    )

    expected = {
        hit_key: CacheHit(record=RECORD),
        missing_key: None,
        null_key: CacheHit(record=None),
        corrupt_key: None,
    }
    assert (
        cache.get_many(
            [hit_key, missing_key, null_key, corrupt_key, hit_key],
            schema=SCHEMA,
        )
        == expected
    )
    assert {key: cache.get(key, schema=SCHEMA) for key in expected} == expected


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


def test_stored_object_schema_mismatch_is_a_miss(
    backend: Backend,
    store: ObjectStore,
    cache: RecordCache,
) -> None:
    stored, _ = store.put("other.schema", RECORD)
    backend.bind(
        key=KEY,
        schema=SCHEMA,
        content_hash=stored.content_hash,
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


@pytest.mark.parametrize(
    ("stored_schema", "stored_content_hash"),
    [
        pytest.param("", "a" * 64, id="empty-schema"),
        pytest.param(SCHEMA, "not-a-hash", id="malformed-content-hash"),
    ],
)
def test_corrupted_binding_is_a_miss(
    backend: Backend,
    cache: RecordCache,
    stored_schema: str,
    stored_content_hash: str,
) -> None:
    backend.bind(
        key=KEY,
        schema=stored_schema,
        content_hash=stored_content_hash,
    )

    assert cache.get(KEY, schema=SCHEMA) is None


class BackendReadError(StoreError):
    pass


class FailingReadBackend(MemoryBackend):
    def get_bound_objects(
        self,
        *,
        keys: tuple[str, ...],
    ) -> dict[str, BoundObjectRow]:
        raise BackendReadError(f"batch storage unavailable for {keys!r}")


def test_backend_read_failure_propagates() -> None:
    backend = FailingReadBackend()
    reference = ObjectReference.for_record(SCHEMA, RECORD)
    backend.bind(
        key=KEY,
        schema=reference.schema,
        content_hash=reference.content_hash,
    )
    cache = RecordCache(ObjectStore(backend))

    with pytest.raises(
        BackendReadError,
    ):
        cache.get(KEY, schema=SCHEMA)


@pytest.mark.parametrize(
    "invalid_schema",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        pytest.param(123, id="integer"),
    ],
)
def test_invalid_requested_schema_raises_before_read(
    invalid_schema: object,
) -> None:
    cache = RecordCache(ObjectStore(FailingReadBackend()))

    with pytest.raises(ReferenceValidationError):
        cache.get(
            KEY,
            schema=invalid_schema,  # ty: ignore[invalid-argument-type]
        )


def test_conflicting_put_returns_the_first_winner(
    cache: RecordCache,
) -> None:
    first = cache.put(KEY, SCHEMA, RECORD)
    second = cache.put(KEY, SCHEMA, OTHER)
    assert second == first
    assert cache.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)


def test_put_many_is_idempotent_and_returns_existing_conflict_winners(
    cache: RecordCache,
) -> None:
    other_key = "example.memo.v1:other"
    entries = {
        KEY: CacheEntry(schema=SCHEMA, record=RECORD),
        other_key: CacheEntry(schema=SCHEMA, record=OTHER),
    }

    first = cache.put_many(entries)
    assert cache.put_many(entries) == first
    conflicted = cache.put_many(
        {
            KEY: CacheEntry(schema=SCHEMA, record=OTHER),
            other_key: CacheEntry(schema=SCHEMA, record=RECORD),
        }
    )

    assert conflicted == first
    assert cache.get_many([KEY, other_key], schema=SCHEMA) == {
        KEY: CacheHit(record=RECORD),
        other_key: CacheHit(record=OTHER),
    }


def test_put_many_prepares_every_record_before_backend_mutation() -> None:
    backend = MemoryBackend()
    cache = RecordCache(ObjectStore(backend))
    valid_key = "batch:valid"
    invalid_key = "batch:invalid"
    valid_reference = ObjectReference.for_record(SCHEMA, RECORD)

    with pytest.raises(StrictJsonError):
        cache.put_many(
            {
                valid_key: CacheEntry(schema=SCHEMA, record=RECORD),
                invalid_key: CacheEntry(
                    schema=SCHEMA,
                    record={"unsupported": {1}},  # ty: ignore[invalid-argument-type]
                ),
            }
        )

    assert backend.get_binding(key=valid_key) is None
    assert backend.get_binding(key=invalid_key) is None
    assert (
        backend.get_object(
            schema=valid_reference.schema,
            content_hash=valid_reference.content_hash,
        )
        is None
    )


def test_put_many_canonicalizes_and_hashes_each_record_once(
    cache: RecordCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonicalized: list[Jsonable] = []
    hashed: list[str] = []
    canonical_json = content_addressing_module.canonical_json
    hash_canonical = content_addressing_module._hash_canonical

    def canonical_spy(record: Jsonable) -> str:
        canonicalized.append(record)
        return canonical_json(record)

    def hash_spy(canonical: str) -> str:
        hashed.append(canonical)
        return hash_canonical(canonical)

    monkeypatch.setattr(
        content_addressing_module, "canonical_json", canonical_spy
    )
    monkeypatch.setattr(content_addressing_module, "_hash_canonical", hash_spy)

    cache.put_many(
        {
            KEY: CacheEntry(schema=SCHEMA, record=RECORD),
            "batch:other": CacheEntry(schema=SCHEMA, record=OTHER),
        }
    )

    assert canonicalized == [RECORD, OTHER]
    assert hashed == [canonical_json(RECORD), canonical_json(OTHER)]


def test_get_many_canonicalizes_and_hashes_each_hit_once(
    cache: RecordCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_key = "batch:other"
    cache.put_many(
        {
            KEY: CacheEntry(schema=SCHEMA, record=RECORD),
            other_key: CacheEntry(schema=SCHEMA, record=OTHER),
        }
    )
    canonicalized: list[Jsonable] = []
    hashed: list[str] = []
    canonical_json = object_store_module.canonical_json
    hash_canonical = object_store_module._hash_canonical

    def canonical_spy(record: Jsonable) -> str:
        canonicalized.append(record)
        return canonical_json(record)

    def hash_spy(canonical: str) -> str:
        hashed.append(canonical)
        return hash_canonical(canonical)

    monkeypatch.setattr(object_store_module, "canonical_json", canonical_spy)
    monkeypatch.setattr(object_store_module, "_hash_canonical", hash_spy)

    assert cache.get_many([KEY, other_key], schema=SCHEMA) == {
        KEY: CacheHit(record=RECORD),
        other_key: CacheHit(record=OTHER),
    }
    assert canonicalized == [RECORD, OTHER]
    assert hashed == [canonical_json(RECORD), canonical_json(OTHER)]
