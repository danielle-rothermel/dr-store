from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from dr_serialize import StrictJsonError
from dr_serialize.canonical import (
    CANONICAL_JSON_MAX_INTEGER_DIGITS,
    JsonEncodeError,
)

import dr_store.object_store as object_store_module
from dr_store import (
    ContentHashMismatchError,
    ContentMismatchReason,
    ObjectConflictError,
    ObjectNotFoundError,
    ObjectReference,
    ObjectStore,
    PutStatus,
    ReferenceValidationError,
    SchemaMismatchError,
    compute_content_hash,
)

if TYPE_CHECKING:
    from dr_serialize import Jsonable

    from tests.object_store.conftest import ControlledBackend

SCHEMA = "example.record"
RECORD: Jsonable = {"payload": {"a": 1, "b": [2, 3]}, "provenance": "x"}


async def test_put_get_replay_and_input_isolation(store: ObjectStore) -> None:
    record: Jsonable = {"payload": {"items": [1, 2]}}
    reference, status = await store.put(SCHEMA, record)
    assert status is PutStatus.STORED
    assert reference.content_hash == compute_content_hash(record)
    assert isinstance(record, dict)
    payload = record["payload"]
    assert isinstance(payload, dict)
    items = payload["items"]
    assert isinstance(items, list)
    items.append(3)
    assert await store.get(reference) == {"payload": {"items": [1, 2]}}
    replay, replay_status = await store.put(
        SCHEMA, {"payload": {"items": [1, 2]}}
    )
    assert replay == reference
    assert replay_status is PutStatus.IDEMPOTENT


async def test_get_missing_and_wrong_schema(store: ObjectStore) -> None:
    missing = ObjectReference.for_record(SCHEMA, {"missing": True})
    with pytest.raises(ObjectNotFoundError):
        await store.get(missing)

    reference, _ = await store.put(SCHEMA, RECORD)
    wrong = ObjectReference("other.schema", reference.content_hash)
    with pytest.raises(SchemaMismatchError):
        await store.get(wrong)


def _controlled_store(
    backend: ControlledBackend,
    reference: ObjectReference,
    canonical: str,
) -> ObjectStore:
    backend.set_object(
        schema=reference.schema,
        content_hash=reference.content_hash,
        canonical=canonical,
    )
    return ObjectStore(backend)


@pytest.mark.parametrize(
    ("canonical", "reason"),
    [
        ('{"tampered":true}', ContentMismatchReason.HASH_MISMATCH),
        ("not-json{{{", ContentMismatchReason.INVALID_JSON),
        ('{"payload":NaN}', ContentMismatchReason.INVALID_JSON),
        ('{"a": 1}', ContentMismatchReason.NON_CANONICAL_FORM),
    ],
)
async def test_get_rejects_corrupt_or_noncanonical_storage(
    controlled_backend: ControlledBackend,
    canonical: str,
    reason: ContentMismatchReason,
) -> None:
    reference = ObjectReference.for_record(SCHEMA, {"a": 1})
    store = _controlled_store(controlled_backend, reference, canonical)
    with pytest.raises(ContentHashMismatchError) as caught:
        await store.get(reference)
    assert caught.value.reason is reason
    if reason is ContentMismatchReason.HASH_MISMATCH:
        assert caught.value.actual is not None
    else:
        assert caught.value.actual is None


@pytest.mark.parametrize(
    "parse_error",
    [ValueError("integer limit"), RecursionError("nesting limit")],
)
async def test_get_translates_parser_failures(
    controlled_backend: ControlledBackend,
    monkeypatch: pytest.MonkeyPatch,
    parse_error: Exception,
) -> None:
    reference = ObjectReference.for_record(SCHEMA, 0)
    store = _controlled_store(controlled_backend, reference, "0")

    def fail_parse(_canonical: str) -> object:
        raise parse_error

    monkeypatch.setattr(object_store_module.json, "loads", fail_parse)
    with pytest.raises(ContentHashMismatchError) as caught:
        await store.get(reference)
    assert caught.value.reason is ContentMismatchReason.INVALID_JSON
    assert caught.value.__cause__ is parse_error


async def test_get_translates_canonical_profile_failure(
    controlled_backend: ControlledBackend,
) -> None:
    canonical = "1" + ("0" * CANONICAL_JSON_MAX_INTEGER_DIGITS)
    reference = ObjectReference(
        SCHEMA, hashlib.sha256(canonical.encode()).hexdigest()
    )
    store = _controlled_store(controlled_backend, reference, canonical)
    with pytest.raises(ContentHashMismatchError) as caught:
        await store.get(reference)
    assert caught.value.reason is ContentMismatchReason.NON_CANONICAL_PROFILE
    assert isinstance(caught.value.__cause__, JsonEncodeError)


async def test_same_content_under_distinct_schemas(store: ObjectStore) -> None:
    first, _ = await store.put("schema.one", {"a": 1})
    second, _ = await store.put("schema.two", {"a": 1})
    assert first.content_hash == second.content_hash
    assert await store.get(first) == {"a": 1}
    assert await store.get(second) == {"a": 1}


async def test_object_collision_does_not_overwrite(
    controlled_backend: ControlledBackend,
) -> None:
    reference = ObjectReference.for_record(SCHEMA, RECORD)
    controlled_backend.set_object(
        schema=SCHEMA,
        content_hash=reference.content_hash,
        canonical="different-canonical",
    )
    with pytest.raises(ObjectConflictError):
        await ObjectStore(controlled_backend).put(SCHEMA, RECORD)


@pytest.mark.parametrize(
    "invalid_record",
    [{"value": float("nan")}, {"value": {1}}],
)
async def test_invalid_record_never_reaches_backend(
    controlled_backend: ControlledBackend,
    invalid_record: object,
) -> None:
    with pytest.raises(StrictJsonError):
        await ObjectStore(controlled_backend).put(
            SCHEMA,
            invalid_record,  # ty: ignore[invalid-argument-type]
        )
    assert controlled_backend.put_calls == 0


async def test_schema_validation_precedes_backend_await(
    controlled_backend: ControlledBackend,
) -> None:
    for invalid in ["", "\0", "prefix\0suffix", "\ud800"]:
        with pytest.raises(ReferenceValidationError):
            await ObjectStore(controlled_backend).put(invalid, RECORD)
    with pytest.raises(JsonEncodeError):
        await ObjectStore(controlled_backend).put(
            SCHEMA, 10**CANONICAL_JSON_MAX_INTEGER_DIGITS
        )
    assert controlled_backend.put_calls == 0
