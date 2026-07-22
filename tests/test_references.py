"""ObjectReference construction, validation, and Content Hash rules."""

from __future__ import annotations

import dataclasses
import hashlib
from typing import TYPE_CHECKING

import pytest
from dr_serialize import canonical_json

from dr_store import (
    CONTENT_HASH_LENGTH,
    ObjectReference,
    ReferenceValidationError,
    compute_content_hash,
    is_content_hash,
)
from dr_store.errors import ContentHashMismatchError

if TYPE_CHECKING:
    from dr_serialize import Jsonable

VALID_HASH = "a" * 64


def test_content_hash_is_full_lowercase_sha256_over_canonical_json() -> None:
    record: Jsonable = {"b": 2, "a": [1, 2, 3]}
    expected = hashlib.sha256(
        canonical_json(record).encode("utf-8")
    ).hexdigest()
    actual = compute_content_hash(record)
    assert actual == expected
    assert len(actual) == CONTENT_HASH_LENGTH
    assert actual == actual.lower()


def test_content_hash_is_key_order_independent() -> None:
    assert compute_content_hash({"a": 1, "b": 2}) == compute_content_hash(
        {"b": 2, "a": 1}
    )


def test_is_content_hash_accepts_only_64_lowercase_hex() -> None:
    assert is_content_hash(VALID_HASH)
    assert not is_content_hash("A" * 64)
    assert not is_content_hash("a" * 63)
    assert not is_content_hash("a" * 65)
    assert not is_content_hash("g" * 64)


def test_reference_rejects_empty_schema() -> None:
    with pytest.raises(ReferenceValidationError):
        ObjectReference(schema="", content_hash=VALID_HASH)


@pytest.mark.parametrize(
    "bad_hash",
    ["A" * 64, "a" * 63, "a" * 65, "z" * 64, "", "not-a-hash"],
)
def test_reference_rejects_malformed_content_hash(bad_hash: str) -> None:
    with pytest.raises(ReferenceValidationError):
        ObjectReference(schema="example.record", content_hash=bad_hash)


def test_reference_is_frozen_and_hashable() -> None:
    ref = ObjectReference.for_record("example.record", {"a": 1})
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(ref, "schema", "other")  # noqa: B010 -- assert frozen
    assert hash(ref) == hash(
        ObjectReference(schema=ref.schema, content_hash=ref.content_hash)
    )


def test_for_record_matches_verify_record() -> None:
    ref = ObjectReference.for_record("example.record", {"a": 1})
    ref.verify_record({"a": 1})
    with pytest.raises(ContentHashMismatchError):
        ref.verify_record({"a": 2})
