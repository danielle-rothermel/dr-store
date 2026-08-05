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
    ObjectConflictError,
    ObjectNotFoundError,
    ObjectReference,
    ObjectStore,
    PutStatus,
    SchemaMismatchError,
    compute_content_hash,
)

if TYPE_CHECKING:
    from dr_serialize import Jsonable

    from tests.object_store.conftest import ControlledBackend

SCHEMA = "example.record"
RECORD: Jsonable = {"payload": {"a": 1, "b": [2, 3]}, "provenance": "x"}
RECORD_REPLAY: Jsonable = {
    "payload": {"a": 1, "b": [2, 3]},
    "provenance": "x",
}
MISSING: Jsonable = {"never": "stored"}
ORDER_A: Jsonable = {"a": 1, "b": 2}
ORDER_B: Jsonable = {"b": 2, "a": 1}


def test_put_returns_typed_reference_with_content_hash(
    store: ObjectStore,
) -> None:
    ref, status = store.put(SCHEMA, RECORD)
    assert status is PutStatus.STORED
    assert ref.schema == SCHEMA
    assert ref.content_hash == compute_content_hash(RECORD)


def test_get_returns_exact_record(store: ObjectStore) -> None:
    ref, _ = store.put(SCHEMA, RECORD)
    assert store.get(ref) == RECORD


def test_mutating_put_input_does_not_mutate_stored_content(
    store: ObjectStore,
) -> None:
    record: Jsonable = {"payload": {"items": [1, 2]}}
    ref, _ = store.put(SCHEMA, record)

    assert isinstance(record, dict)
    payload = record["payload"]
    assert isinstance(payload, dict)
    items = payload["items"]
    assert isinstance(items, list)
    items.append(3)

    assert store.get(ref) == {"payload": {"items": [1, 2]}}


def test_mutating_get_result_does_not_mutate_stored_content(
    store: ObjectStore,
) -> None:
    ref, _ = store.put(SCHEMA, {"payload": {"items": [1, 2]}})
    returned = store.get(ref)

    assert isinstance(returned, dict)
    payload = returned["payload"]
    assert isinstance(payload, dict)
    items = payload["items"]
    assert isinstance(items, list)
    items.append(3)

    assert store.get(ref) == {"payload": {"items": [1, 2]}}


def test_identical_put_is_idempotent_success(store: ObjectStore) -> None:
    ref1, status1 = store.put(SCHEMA, RECORD)
    ref2, status2 = store.put(SCHEMA, RECORD_REPLAY)
    assert status1 is PutStatus.STORED
    assert status2 is PutStatus.IDEMPOTENT
    assert ref1 == ref2


def test_key_order_variation_is_still_idempotent(
    store: ObjectStore,
) -> None:
    _, status1 = store.put(SCHEMA, ORDER_A)
    _, status2 = store.put(SCHEMA, ORDER_B)
    assert status1 is PutStatus.STORED
    assert status2 is PutStatus.IDEMPOTENT


def test_get_missing_reference_raises(store: ObjectStore) -> None:
    ref = ObjectReference.for_record(SCHEMA, MISSING)
    with pytest.raises(ObjectNotFoundError):
        store.get(ref)


def test_get_with_wrong_schema_raises_schema_mismatch(
    store: ObjectStore,
) -> None:
    ref, _ = store.put(SCHEMA, RECORD)
    wrong = ObjectReference(
        schema="other.schema",
        content_hash=ref.content_hash,
    )
    with pytest.raises(SchemaMismatchError) as excinfo:
        store.get(wrong)
    assert excinfo.value.expected == "other.schema"
    assert excinfo.value.actual == SCHEMA


def _store_with_controlled_canonical(
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


def test_get_detects_corrupted_content(
    controlled_backend: ControlledBackend,
) -> None:
    ref = ObjectReference.for_record(SCHEMA, RECORD)
    store = _store_with_controlled_canonical(
        controlled_backend,
        ref,
        '{"tampered":true}',
    )
    with pytest.raises(ContentHashMismatchError):
        store.get(ref)


def test_get_detects_non_json_corruption(
    controlled_backend: ControlledBackend,
) -> None:
    ref = ObjectReference.for_record(SCHEMA, RECORD)
    store = _store_with_controlled_canonical(
        controlled_backend,
        ref,
        "not-json{{{",
    )
    with pytest.raises(ContentHashMismatchError):
        store.get(ref)


def test_get_detects_non_finite_corruption(
    controlled_backend: ControlledBackend,
) -> None:
    # json.loads accepts NaN, so strict JSON validation fails after parsing.
    ref = ObjectReference.for_record(SCHEMA, RECORD)
    store = _store_with_controlled_canonical(
        controlled_backend,
        ref,
        '{"payload":NaN}',
    )
    with pytest.raises(ContentHashMismatchError):
        store.get(ref)


@pytest.mark.parametrize(
    "parse_error",
    [
        pytest.param(
            ValueError("integer conversion limit"),
            id="integer-conversion-limit",
        ),
        pytest.param(
            RecursionError("nesting limit"),
            id="nesting-limit",
        ),
    ],
)
def test_get_translates_stored_json_parser_failures(
    controlled_backend: ControlledBackend,
    monkeypatch: pytest.MonkeyPatch,
    parse_error: Exception,
) -> None:
    ref = ObjectReference.for_record(SCHEMA, 0)
    store = _store_with_controlled_canonical(
        controlled_backend,
        ref,
        "0",
    )

    def fail_parse(_canonical: str) -> object:
        raise parse_error

    monkeypatch.setattr(object_store_module.json, "loads", fail_parse)

    with pytest.raises(ContentHashMismatchError) as caught:
        store.get(ref)

    assert caught.value.__cause__ is parse_error


def test_get_detects_non_canonical_bytes(
    controlled_backend: ControlledBackend,
) -> None:
    # Whitespace changes the stored bytes without changing the decoded value.
    ref = ObjectReference.for_record(SCHEMA, {"a": 1})
    store = _store_with_controlled_canonical(
        controlled_backend,
        ref,
        '{"a": 1}',
    )
    with pytest.raises(ContentHashMismatchError):
        store.get(ref)


def test_get_translates_stored_content_outside_the_canonical_profile(
    controlled_backend: ControlledBackend,
) -> None:
    canonical = "1" + ("0" * CANONICAL_JSON_MAX_INTEGER_DIGITS)
    ref = ObjectReference(
        schema=SCHEMA,
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )
    store = _store_with_controlled_canonical(
        controlled_backend,
        ref,
        canonical,
    )

    with pytest.raises(ContentHashMismatchError) as caught:
        store.get(ref)

    assert isinstance(caught.value.__cause__, JsonEncodeError)


def test_same_content_under_different_schema_both_store(
    store: ObjectStore,
) -> None:
    ref_one, status_one = store.put("schema.one", {"a": 1})
    ref_two, status_two = store.put("schema.two", {"a": 1})
    assert status_one is PutStatus.STORED
    assert status_two is PutStatus.STORED
    assert ref_one.content_hash == ref_two.content_hash
    assert ref_one.schema != ref_two.schema
    assert store.get(ref_one) == {"a": 1}
    assert store.get(ref_two) == {"a": 1}


def test_different_content_at_same_hash_conflicts_without_overwrite(
    controlled_backend: ControlledBackend,
) -> None:
    # Populate the schema and content-hash pair with collision bytes.
    ref = ObjectReference.for_record(SCHEMA, RECORD)
    controlled_backend.set_object(
        schema=ref.schema,
        content_hash=ref.content_hash,
        canonical="different-canonical",
    )
    store = ObjectStore(controlled_backend)
    with pytest.raises(ObjectConflictError):
        store.put(SCHEMA, RECORD)
    assert controlled_backend.object_row == (
        ref.schema,
        ref.content_hash,
        "different-canonical",
    )


@pytest.mark.parametrize(
    "invalid_record",
    [
        pytest.param({"value": float("nan")}, id="non-finite"),
        pytest.param({"value": {1}}, id="unsupported-type"),
    ],
)
def test_invalid_strict_json_never_reaches_backend(
    controlled_backend: ControlledBackend,
    invalid_record: object,
) -> None:
    store = ObjectStore(controlled_backend)
    with pytest.raises(StrictJsonError):
        compute_content_hash(
            invalid_record  # ty: ignore[invalid-argument-type]
        )
    with pytest.raises(StrictJsonError):
        store.put(
            SCHEMA,
            invalid_record,  # ty: ignore[invalid-argument-type]
        )
    assert controlled_backend.put_calls == 0
    assert controlled_backend.object_row is None


def test_canonical_profile_violation_never_reaches_backend(
    controlled_backend: ControlledBackend,
) -> None:
    record = 10**CANONICAL_JSON_MAX_INTEGER_DIGITS
    store = ObjectStore(controlled_backend)

    with pytest.raises(JsonEncodeError):
        store.put(SCHEMA, record)

    assert controlled_backend.put_calls == 0
    assert controlled_backend.object_row is None
