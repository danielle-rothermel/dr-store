from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from dr_serialize import StrictJsonError

from dr_store import (
    Backend,
    BoundObjectRow,
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

KEY = "example.memo.v1:key"
SCHEMA = "example.record"
RECORD: Jsonable = {"payload": {"a": 1, "b": [2, 3]}}
OTHER: Jsonable = {"payload": "different"}


@pytest.fixture
def cache(store: ObjectStore) -> RecordCache:
    return RecordCache(store)


def test_derived_key_uses_canonical_hash_and_valid_text() -> None:
    assert derive_cache_key("ns.v1", RECORD) == (
        f"ns.v1:{compute_content_hash(RECORD)}"
    )
    with pytest.raises(TypeError):
        derive_cache_key(None, RECORD)  # ty: ignore[invalid-argument-type]
    for invalid in ["\0", "\ud800"]:
        with pytest.raises(ReferenceValidationError):
            derive_cache_key(invalid, RECORD)


async def test_round_trip_null_miss_and_first_winner(
    cache: RecordCache,
) -> None:
    assert await cache.get(KEY, schema=SCHEMA) is None
    first = await cache.put(KEY, SCHEMA, RECORD)
    second = await cache.put(KEY, SCHEMA, OTHER)
    assert second == first
    assert await cache.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)
    assert await cache.get(KEY, schema="other.schema") is None
    assert await cache.put("null", SCHEMA, None)
    assert await cache.get("null", schema=SCHEMA) == CacheHit(record=None)


async def test_batch_reports_distinct_hits_misses_and_corruption(
    backend: Backend,
    cache: RecordCache,
) -> None:
    await cache.put_many(
        {
            KEY: CacheEntry(SCHEMA, RECORD),
            "null": CacheEntry(SCHEMA, None),
        }
    )
    corrupt = ObjectReference.for_record(SCHEMA, OTHER)
    await backend.put_object(
        schema=SCHEMA,
        content_hash=corrupt.content_hash,
        canonical='{"tampered":true}',
    )
    await backend.bind(
        key="corrupt", schema=SCHEMA, content_hash=corrupt.content_hash
    )
    assert await cache.get_many(
        [KEY, "missing", "null", "corrupt", KEY], schema=SCHEMA
    ) == {
        KEY: CacheHit(record=RECORD),
        "missing": None,
        "null": CacheHit(record=None),
        "corrupt": None,
    }
    assert cache.stats.corruption_count == 1


async def test_missing_object_is_a_miss(
    backend: Backend,
    cache: RecordCache,
) -> None:
    reference = ObjectReference.for_record(SCHEMA, RECORD)
    await backend.bind(
        key=KEY,
        schema=reference.schema,
        content_hash=reference.content_hash,
    )
    assert await cache.get(KEY, schema=SCHEMA) is None


class CorruptBindingBackend(MemoryBackend):
    async def get_bound_objects(
        self, *, keys: tuple[str, ...]
    ) -> dict[str, BoundObjectRow]:
        assert keys == ("bad-binding",)
        return {
            "bad-binding": BoundObjectRow(
                binding_schema=SCHEMA,
                binding_content_hash="not-a-hash",
                canonical=None,
            )
        }


async def test_controlled_corrupt_binding_is_a_cache_miss() -> None:
    cache = RecordCache(ObjectStore(CorruptBindingBackend()))
    assert await cache.get("bad-binding", schema=SCHEMA) is None
    assert cache.stats.corruption_count == 1


class BackendReadError(StoreError):
    pass


class FailingReadBackend(MemoryBackend):
    async def get_bound_objects(
        self, *, keys: tuple[str, ...]
    ) -> dict[str, BoundObjectRow]:
        raise BackendReadError(f"batch storage unavailable for {keys!r}")


async def test_backend_failure_propagates_but_validation_precedes_it() -> None:
    cache = RecordCache(ObjectStore(FailingReadBackend()))
    with pytest.raises(BackendReadError):
        await cache.get(KEY, schema=SCHEMA)
    for invalid_schema in ["", "\0", "\ud800"]:
        with pytest.raises(ReferenceValidationError):
            await cache.get(KEY, schema=invalid_schema)
    for invalid_key in ["\0", "\ud800"]:
        with pytest.raises(ReferenceValidationError):
            await cache.get(invalid_key, schema=SCHEMA)


async def test_put_many_prepares_every_entry_before_backend_mutation() -> None:
    backend = MemoryBackend()
    cache = RecordCache(ObjectStore(backend))
    with pytest.raises(StrictJsonError):
        await cache.put_many(
            {
                "valid": CacheEntry(SCHEMA, RECORD),
                "invalid": CacheEntry(
                    SCHEMA,
                    {"unsupported": {1}},  # ty: ignore[invalid-argument-type]
                ),
            }
        )
    assert await backend.get_binding(key="valid") is None


async def test_put_paths_validate_all_text_before_backend_mutation() -> None:
    backend = MemoryBackend()
    cache = RecordCache(ObjectStore(backend))
    for invalid_key in ["\0", "prefix\0suffix", "\ud800"]:
        with pytest.raises(ReferenceValidationError):
            await cache.put(invalid_key, SCHEMA, RECORD)
    for invalid_schema in ["", "\0", "\ud800"]:
        with pytest.raises(ReferenceValidationError):
            await cache.put(KEY, invalid_schema, RECORD)
    with pytest.raises(ReferenceValidationError):
        await cache.put_many(
            {
                "valid": CacheEntry(SCHEMA, RECORD),
                "\ud800": CacheEntry(SCHEMA, OTHER),
            }
        )
    assert await backend.get_binding(key="valid") is None


async def test_batch_prepares_and_verifies_each_record_once(
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
    entries = {
        KEY: CacheEntry(SCHEMA, RECORD),
        "other": CacheEntry(SCHEMA, OTHER),
    }
    await cache.put_many(entries)
    assert canonicalized == [RECORD, OTHER]
    assert hashed == [canonical_json(RECORD), canonical_json(OTHER)]

    canonicalized.clear()
    hashed.clear()
    monkeypatch.setattr(object_store_module, "canonical_json", canonical_spy)
    monkeypatch.setattr(object_store_module, "_hash_canonical", hash_spy)
    assert await cache.get_many(entries, schema=SCHEMA) == {
        KEY: CacheHit(RECORD),
        "other": CacheHit(OTHER),
    }
    assert canonicalized == [RECORD, OTHER]
    assert hashed == [canonical_json(RECORD), canonical_json(OTHER)]
