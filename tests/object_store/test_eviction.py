from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dr_store import (
    BindStatus,
    EvictStatus,
    ObjectStore,
    ReferenceValidationError,
)

if TYPE_CHECKING:
    from dr_serialize import Jsonable

SCHEMA = "example.record"
RECORD_A: Jsonable = {"payload": {"id": "a"}}
RECORD_B: Jsonable = {"payload": {"id": "b"}}
KEY_A = "key:a"
KEY_B = "key:b"
KEY_MISSING = "key:missing"


async def test_evicted_key_stops_resolving(store: ObjectStore) -> None:
    reference, _ = await store.put(SCHEMA, RECORD_A)
    await store.bind(KEY_A, reference)
    assert await store.resolve(KEY_A) == reference

    assert await store.evict_bindings([KEY_A]) == {KEY_A: EvictStatus.EVICTED}
    assert await store.resolve(KEY_A) is None
    assert await store.get_many([KEY_A], schema=SCHEMA) == {KEY_A: None}


async def test_eviction_removes_resolvability_never_content(
    store: ObjectStore,
) -> None:
    reference, _ = await store.put(SCHEMA, RECORD_A)
    await store.bind(KEY_A, reference)
    await store.evict_bindings([KEY_A])

    # Content is addressed, potentially shared, and never deleted by eviction.
    assert await store.get(reference) == RECORD_A


async def test_eviction_leaves_other_keys_on_shared_content(
    store: ObjectStore,
) -> None:
    reference, _ = await store.put(SCHEMA, RECORD_A)
    await store.bind(KEY_A, reference)
    await store.bind(KEY_B, reference)

    assert await store.evict_bindings([KEY_A]) == {KEY_A: EvictStatus.EVICTED}
    assert await store.resolve(KEY_A) is None
    assert await store.resolve(KEY_B) == reference
    assert await store.get(reference) == RECORD_A


async def test_absent_key_reports_absent_and_replay_is_idempotent(
    store: ObjectStore,
) -> None:
    assert await store.evict_bindings([KEY_MISSING]) == {
        KEY_MISSING: EvictStatus.ABSENT
    }

    reference, _ = await store.put(SCHEMA, RECORD_A)
    await store.bind(KEY_A, reference)
    assert await store.evict_bindings([KEY_A]) == {KEY_A: EvictStatus.EVICTED}
    assert await store.evict_bindings([KEY_A]) == {KEY_A: EvictStatus.ABSENT}


async def test_mixed_batch_reports_per_key_status(store: ObjectStore) -> None:
    first, _ = await store.put(SCHEMA, RECORD_A)
    second, _ = await store.put(SCHEMA, RECORD_B)
    await store.bind(KEY_A, first)
    await store.bind(KEY_B, second)

    assert await store.evict_bindings([KEY_A, KEY_MISSING, KEY_B, KEY_A]) == {
        KEY_A: EvictStatus.EVICTED,
        KEY_MISSING: EvictStatus.ABSENT,
        KEY_B: EvictStatus.EVICTED,
    }
    assert await store.resolve(KEY_A) is None
    assert await store.resolve(KEY_B) is None


async def test_evicted_key_is_bindable_again(store: ObjectStore) -> None:
    first, _ = await store.put(SCHEMA, RECORD_A)
    second, _ = await store.put(SCHEMA, RECORD_B)
    await store.bind(KEY_A, first)
    await store.evict_bindings([KEY_A])

    assert await store.bind(KEY_A, second) is BindStatus.BOUND
    assert await store.resolve(KEY_A) == second


async def test_empty_batch_touches_no_bindings(store: ObjectStore) -> None:
    reference, _ = await store.put(SCHEMA, RECORD_A)
    await store.bind(KEY_A, reference)

    assert await store.evict_bindings([]) == {}
    assert await store.resolve(KEY_A) == reference


@pytest.mark.parametrize("value", ["\0", "key\0tail", "\ud800"])
async def test_eviction_rejects_invalid_key_before_storage(
    store: ObjectStore, value: str
) -> None:
    reference, _ = await store.put(SCHEMA, RECORD_A)
    await store.bind(KEY_A, reference)

    with pytest.raises(ReferenceValidationError):
        await store.evict_bindings([KEY_A, value])
    assert await store.resolve(KEY_A) == reference
