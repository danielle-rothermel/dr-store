from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dr_serialize import (
    Jsonable,
    canonical_json,
    validate_strict_json,
)

from dr_store.core.errors import (
    ContentHashMismatchError,
    ContentMismatchReason,
    ReferenceValidationError,
)

CONTENT_HASH_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
OBJECT_REFERENCE_PREFIX = "dr-store-object:v1"


@dataclass(frozen=True, slots=True)
class _PreparedRecord:
    canonical: str
    content_hash: str


def _hash_canonical(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prepare_record(record: Jsonable) -> _PreparedRecord:
    validated = validate_strict_json(record)
    canonical = canonical_json(validated)
    return _PreparedRecord(
        canonical=canonical,
        content_hash=_hash_canonical(canonical),
    )


def validate_reference_schema(schema: object) -> str:
    if not isinstance(schema, str) or not schema:
        raise ReferenceValidationError(
            "ObjectReference schema must be a non-empty string"
        )
    _validate_storage_text(schema, name="ObjectReference schema")
    return schema


def validate_binding_key(key: object) -> str:
    if not isinstance(key, str):
        raise ReferenceValidationError("binding key must be a string")
    _validate_storage_text(key, name="binding key")
    return key


def validate_content_hash(content_hash: object) -> str:
    if not isinstance(content_hash, str) or not is_content_hash(content_hash):
        raise ReferenceValidationError(
            "content hash must be a 64-character lowercase hex SHA-256 hash, "
            f"got {content_hash!r}"
        )
    return content_hash


def _validate_storage_text(value: str, *, name: str) -> None:
    if "\0" in value or any(
        "\ud800" <= character <= "\udfff" for character in value
    ):
        raise ReferenceValidationError(
            f"{name} must not contain NUL or unpaired surrogate code points"
        )


def is_content_hash(value: str) -> bool:
    return len(value) == CONTENT_HASH_LENGTH and all(
        char in _HEX_DIGITS for char in value
    )


def compute_content_hash(record: Jsonable) -> str:
    """Validate ``record`` and hash it with dr-serialize's canonical JSON."""
    return _prepare_record(record).content_hash


@dataclass(frozen=True, slots=True)
class ObjectReference:
    """A validated ``(schema, content_hash)`` reference."""

    schema: str
    content_hash: str

    def __post_init__(self) -> None:
        validate_reference_schema(self.schema)
        validate_content_hash(self.content_hash)

    @classmethod
    def for_record(cls, schema: str, record: Jsonable) -> ObjectReference:
        return cls(schema=schema, content_hash=compute_content_hash(record))

    def verify_record(self, record: Jsonable) -> None:
        """Raise if ``record`` does not match this reference's content hash."""
        actual = compute_content_hash(record)
        if actual != self.content_hash:
            raise ContentHashMismatchError(
                expected=self.content_hash,
                actual=actual,
                schema=self.schema,
                reason=ContentMismatchReason.HASH_MISMATCH,
            )


def format_object_reference(reference: ObjectReference) -> str:
    """Format a validated reference as its pinned opaque wire string."""
    return (
        f"{OBJECT_REFERENCE_PREFIX}:{reference.schema}:"
        f"{reference.content_hash}"
    )


def parse_object_reference(value: str) -> ObjectReference:
    """Parse a pinned opaque wire string into a validated reference."""
    if not isinstance(value, str):
        raise ReferenceValidationError(
            "object reference wire string must be a string"
        )
    prefix = f"{OBJECT_REFERENCE_PREFIX}:"
    if not value.startswith(prefix):
        raise ReferenceValidationError(
            "object reference wire string has an unsupported prefix"
        )
    remainder = value.removeprefix(prefix)
    schema, separator, content_hash = remainder.rpartition(":")
    if not separator or not schema or not content_hash:
        raise ReferenceValidationError(
            "object reference wire string is malformed"
        )
    return ObjectReference(schema=schema, content_hash=content_hash)


import dr_store.core.store_errors as _store_errors_module  # noqa: E402

_store_errors_module.ObjectReference = ObjectReference
