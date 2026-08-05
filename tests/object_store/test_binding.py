from __future__ import annotations

import pytest

from dr_store import (
    BindingConflictError,
    BindStatus,
    ObjectReference,
    ObjectStore,
)

KEY = "caller-owned-opaque-key"
REF_A = ObjectReference.for_record("example.record", {"which": "A"})
REF_B = ObjectReference.for_record("example.record", {"which": "B"})


def test_unbound_key_binds(store: ObjectStore) -> None:
    assert store.resolve(KEY) is None
    assert store.bind(KEY, REF_A) is BindStatus.BOUND
    assert store.resolve(KEY) == REF_A


def test_same_reference_replay_is_idempotent(store: ObjectStore) -> None:
    store.bind(KEY, REF_A)
    assert store.bind(KEY, REF_A) is BindStatus.IDEMPOTENT
    assert store.resolve(KEY) == REF_A


def test_different_reference_conflicts_and_keeps_winner(
    store: ObjectStore,
) -> None:
    store.bind(KEY, REF_A)
    with pytest.raises(BindingConflictError) as excinfo:
        store.bind(KEY, REF_B)
    assert excinfo.value.existing == REF_A
    assert excinfo.value.requested == REF_B
    assert store.resolve(KEY) == REF_A


def test_binding_key_is_opaque_arbitrary_string(store: ObjectStore) -> None:
    for key in ["", "a/b/c", "key with spaces", "🔑", "1234", "\n\t"]:
        assert store.bind(key, REF_A) is BindStatus.BOUND
        assert store.resolve(key) == REF_A


def test_distinct_keys_bind_independently(store: ObjectStore) -> None:
    assert store.bind("k1", REF_A) is BindStatus.BOUND
    assert store.bind("k2", REF_B) is BindStatus.BOUND
    assert store.resolve("k1") == REF_A
    assert store.resolve("k2") == REF_B
