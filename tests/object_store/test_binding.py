from __future__ import annotations

import pytest

from dr_store import (
    BindingConflictError,
    BindStatus,
    ObjectReference,
    ObjectStore,
    ReferenceValidationError,
)

KEY = "caller-owned-opaque-key"
REF_A = ObjectReference.for_record("example.record", {"which": "A"})
REF_B = ObjectReference.for_record("example.record", {"which": "B"})


async def test_binding_is_single_assignment(store: ObjectStore) -> None:
    assert await store.resolve(KEY) is None
    assert await store.bind(KEY, REF_A) is BindStatus.BOUND
    assert await store.bind(KEY, REF_A) is BindStatus.IDEMPOTENT
    with pytest.raises(BindingConflictError) as caught:
        await store.bind(KEY, REF_B)
    assert caught.value.existing == REF_A
    assert await store.resolve(KEY) == REF_A


async def test_binding_key_shared_text_domain(store: ObjectStore) -> None:
    for key in ["", "a/b/c", "key with spaces", "🔑", "1234", "\n\t"]:
        assert await store.bind(key, REF_A) is BindStatus.BOUND
        assert await store.resolve(key) == REF_A

    for invalid in ["\0", "prefix\0suffix", "\ud800"]:
        with pytest.raises(ReferenceValidationError):
            await store.bind(invalid, REF_A)
        with pytest.raises(ReferenceValidationError):
            await store.resolve(invalid)
