"""ObjectReference construction, validation, and Content Hash rules."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from dr_serialize import StrictJsonError

from dr_store import (
    CONTENT_HASH_LENGTH,
    ContentHashMismatchError,
    ObjectReference,
    ReferenceValidationError,
    compute_content_hash,
    is_content_hash,
)

if TYPE_CHECKING:
    from dr_serialize import Jsonable

VALID_HASH = "a" * 64


def test_content_hash_is_exact_canonical_sha256() -> None:
    record: Jsonable = {"b": 2, "a": [1, 2, 3]}
    actual = compute_content_hash(record)
    assert (
        actual
        == "17df395fb77661fb2f96417b64819b03367b9a00303e18b0445ac09534f134e1"
    )
    assert actual == compute_content_hash({"a": [1, 2, 3], "b": 2})
    assert len(actual) == CONTENT_HASH_LENGTH
    assert actual == actual.lower()


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(object(), id="non-json-object"),
    ],
)
def test_content_hash_rejects_non_strict_json(record: object) -> None:
    with pytest.raises(StrictJsonError):
        compute_content_hash(record)  # ty: ignore[invalid-argument-type]


def test_is_content_hash_accepts_only_64_lowercase_hex() -> None:
    assert is_content_hash(VALID_HASH)
    assert not is_content_hash("A" * 64)
    assert not is_content_hash("a" * 63)
    assert not is_content_hash("a" * 65)
    assert not is_content_hash("g" * 64)


@pytest.mark.parametrize(
    "bad_schema",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        pytest.param(123, id="integer"),
        pytest.param(b"bytes", id="bytes"),
    ],
)
def test_reference_rejects_invalid_schema(bad_schema: object) -> None:
    with pytest.raises(ReferenceValidationError):
        ObjectReference(
            schema=bad_schema,  # ty: ignore[invalid-argument-type]
            content_hash=VALID_HASH,
        )


@pytest.mark.parametrize(
    "bad_hash",
    [
        pytest.param("A" * 64, id="uppercase"),
        pytest.param("a" * 63, id="short"),
        pytest.param("a" * 65, id="long"),
        pytest.param("z" * 64, id="non-hex"),
        pytest.param("", id="empty"),
        pytest.param("not-a-hash", id="malformed"),
        pytest.param(None, id="none"),
        pytest.param(123, id="integer"),
        pytest.param(b"a" * 64, id="bytes"),
    ],
)
def test_reference_rejects_invalid_content_hash(bad_hash: object) -> None:
    with pytest.raises(ReferenceValidationError):
        ObjectReference(
            schema="example.record",
            content_hash=bad_hash,  # ty: ignore[invalid-argument-type]
        )


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
