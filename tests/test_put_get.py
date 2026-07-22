"""Immutable put and verified get, against every backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dr_store import (
    ContentHashMismatchError,
    ObjectConflictError,
    ObjectNotFoundError,
    ObjectReference,
    PutStatus,
    SchemaMismatchError,
    compute_content_hash,
)

if TYPE_CHECKING:
    from dr_serialize import Jsonable

    from dr_store import ObjectStore

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
    with pytest.raises(SchemaMismatchError):
        store.get(wrong)


def test_get_detects_corrupted_content(store: ObjectStore) -> None:
    ref, _ = store.put(SCHEMA, RECORD)
    # Corrupt the stored canonical text out from under the reference; the
    # verified read must recompute the hash and reject the mismatch.
    # Corrupt to *valid but different* JSON: the hash recompute rejects it.
    _overwrite_stored_canonical(store, ref, '{"tampered": true}')
    with pytest.raises(ContentHashMismatchError):
        store.get(ref)


def _overwrite_stored_canonical(
    store: ObjectStore,
    ref: ObjectReference,
    canonical: str,
) -> None:
    backend = store._backend
    from dr_store import MemoryBackend, SqliteBackend

    if isinstance(backend, MemoryBackend):
        backend._objects[(ref.schema, ref.content_hash)] = canonical
    elif isinstance(backend, SqliteBackend):
        conn = backend._conn
        conn.execute(
            "UPDATE objects SET canonical = ? "
            "WHERE schema = ? AND content_hash = ?",
            (canonical, ref.schema, ref.content_hash),
        )
    else:  # pragma: no cover - defensive
        raise TypeError("unknown backend")


def test_get_detects_non_json_corruption(store: ObjectStore) -> None:
    # Corrupt the stored canonical text into bytes that do not even parse as
    # JSON (bit rot, truncation, a partial write). The verified read must
    # surface this as a typed contract error, never a bare JSONDecodeError.
    ref, _ = store.put(SCHEMA, RECORD)
    _overwrite_stored_canonical(store, ref, "not-json{{{")
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


def test_different_content_at_same_hash_conflicts(
    store: ObjectStore,
) -> None:
    # Simulate a SHA-256 collision: poison the backend so the content-hash
    # key holds *different* canonical content, then prove a contract put of
    # the real record raises rather than overwriting the poisoned row.
    ref = ObjectReference.for_record(SCHEMA, RECORD)
    _poison_stored_object(store, ref, canonical="different-canonical")
    with pytest.raises(ObjectConflictError):
        store.put(SCHEMA, RECORD)


def _poison_stored_object(
    store: ObjectStore,
    ref: ObjectReference,
    *,
    canonical: str,
) -> None:
    outcome = store._backend.put_object(
        schema=ref.schema,
        content_hash=ref.content_hash,
        canonical=canonical,
    )
    assert outcome.inserted
