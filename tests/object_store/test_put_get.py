"""Object Store immutable put and verified get, against every backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from dr_serialize import StrictJsonError

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
# A distinct object with identical canonical content, for replay tests.
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
    # Same canonical value under a different insertion order.
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
    # Corrupt the stored canonical text into bytes that do not even parse as
    # JSON (bit rot, truncation, a partial write). The verified read must
    # surface this as a typed contract error, never a bare JSONDecodeError.
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
    # json.loads accepts NaN/Infinity, so a poisoned canonical text carrying
    # a non-finite token parses yet fails strict validation. The verified
    # read must surface this as a typed contract error, never a leaked
    # StrictJsonError.
    ref = ObjectReference.for_record(SCHEMA, RECORD)
    store = _store_with_controlled_canonical(
        controlled_backend,
        ref,
        '{"payload":NaN}',
    )
    with pytest.raises(ContentHashMismatchError):
        store.get(ref)


def test_get_detects_non_canonical_bytes(
    controlled_backend: ControlledBackend,
) -> None:
    # Byte-level drift that still decodes and hashes identically is
    # corruption: {"a": 1} (with a space) decodes to the same value as the
    # canonical {"a":1}, so verify_record passes, but the raw bytes differ
    # from the canonical form and an idempotent put replay would reject them.
    ref = ObjectReference.for_record(SCHEMA, {"a": 1})
    store = _store_with_controlled_canonical(
        controlled_backend,
        ref,
        '{"a": 1}',
    )
    with pytest.raises(ContentHashMismatchError):
        store.get(ref)


def test_same_content_under_different_schema_both_store(
    store: ObjectStore,
) -> None:
    # The typed key is (schema, content_hash): identical content filed under
    # two different schemas are two distinct objects. Both puts succeed and
    # each resolves to its own record; neither is a spurious conflict.
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
    # Simulate a SHA-256 collision: poison the backend so the content-hash
    # key holds *different* canonical content, then prove a contract put of
    # the real record raises rather than overwriting the poisoned row.
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
