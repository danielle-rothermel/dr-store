"""Object Store: the exact four-row key-to-reference binding table.

Rows mirror the design table one-to-one:

    | existing state | requested | required result           |
    | Unbound        | any ref   | Bind                      |
    | Bound to A     | A         | Same (idempotent success) |
    | Bound to A     | B         | Conflict (keep A)         |
    | Bound to A     | overwrite | no such path exists       |
"""

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


def test_row1_unbound_binds(store: ObjectStore) -> None:
    assert store.resolve(KEY) is None
    assert store.bind(KEY, REF_A) is BindStatus.BOUND
    assert store.resolve(KEY) == REF_A


def test_row2_same_reference_is_idempotent(store: ObjectStore) -> None:
    store.bind(KEY, REF_A)
    assert store.bind(KEY, REF_A) is BindStatus.IDEMPOTENT
    assert store.resolve(KEY) == REF_A


def test_row3_different_reference_conflicts_and_keeps_winner(
    store: ObjectStore,
) -> None:
    store.bind(KEY, REF_A)
    with pytest.raises(BindingConflictError) as excinfo:
        store.bind(KEY, REF_B)
    assert excinfo.value.existing == REF_A
    assert excinfo.value.requested == REF_B
    # The durable winner is preserved unchanged.
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
