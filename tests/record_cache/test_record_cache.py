from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from dr_serialize import StrictJsonError

from dr_store import (
    CacheHit,
    MemoryBackend,
    ObjectReference,
    ObjectStore,
    RecordCache,
    ReferenceValidationError,
    StoreError,
    derive_cache_key,
)

if TYPE_CHECKING:
    from dr_serialize import Jsonable

    from dr_store import Backend

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
    assert cache.get(KEY, schema=SCHEMA) == CacheHit(record=RECORD)


def test_null_record_is_distinct_from_a_miss(cache: RecordCache) -> None:
    cache.put(KEY, SCHEMA, None)

    assert cache.get(KEY, schema=SCHEMA) == CacheHit(record=None)
    assert cache.get("unbound", schema=SCHEMA) is None


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
    def __init__(self, failure_stage: str) -> None:
        super().__init__()
        self._failure_stage = failure_stage

    def get_binding(self, *, key: str) -> tuple[str, str] | None:
        if self._failure_stage == "binding":
            raise BackendReadError(f"binding storage unavailable for {key!r}")
        return super().get_binding(key=key)

    def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        if self._failure_stage == "object":
            raise BackendReadError(
                f"object storage unavailable for {schema!r}"
            )
        return super().get_object(
            schema=schema,
            content_hash=content_hash,
        )


@pytest.mark.parametrize("failure_stage", ["binding", "object"])
def test_backend_read_failure_propagates(failure_stage: str) -> None:
    backend = FailingReadBackend(failure_stage)
    reference = ObjectReference.for_record(SCHEMA, RECORD)
    backend.bind(
        key=KEY,
        schema=reference.schema,
        content_hash=reference.content_hash,
    )
    cache = RecordCache(ObjectStore(backend))

    with pytest.raises(
        BackendReadError,
        match=f"{failure_stage} storage unavailable",
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
    cache = RecordCache(ObjectStore(FailingReadBackend("binding")))

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
