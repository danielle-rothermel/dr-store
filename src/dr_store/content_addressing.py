"""Typed Object Reference and Content Hash computation.

An :class:`ObjectReference` is the typed content-addressed key of the
Object Store: a declared record ``schema`` plus the full 64-character
lowercase SHA-256 ``content_hash`` of the complete canonical persisted
record. The Content Hash is computed through dr-serialize's canonical JSON
lane -- dr-store never invents a second canonicalization dialect -- and is
deliberately distinct from an Identity Hash over Canonical Identity JSON.
"""

from __future__ import annotations

from dataclasses import dataclass

from dr_serialize import (
    Jsonable,
    json_hash,
    validate_strict_json,
)

from dr_store.core.errors import (
    ContentHashMismatchError,
    ReferenceValidationError,
)

CONTENT_HASH_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


def is_content_hash(value: str) -> bool:
    """Return whether ``value`` is a 64-character lowercase hex hash."""
    return len(value) == CONTENT_HASH_LENGTH and all(
        char in _HEX_DIGITS for char in value
    )


def compute_content_hash(record: Jsonable) -> str:
    """Return the Content Hash of a complete canonical persisted record.

    ``record`` must be strict finite JSON; it is validated before hashing so
    that a non-JSON or non-finite value fails loudly rather than producing a
    hash that no later read could reproduce. Canonicalization and the hash
    itself come entirely from dr-serialize.
    """
    return json_hash(validate_strict_json(record))


@dataclass(frozen=True, slots=True)
class ObjectReference:
    """Typed content-addressed reference: ``(schema, content_hash)``.

    ``schema`` is the declared record schema; ``content_hash`` is the full
    64-character lowercase SHA-256 hash of the complete canonical
    persisted record. Both components are validated at construction so an
    ill-formed reference can never enter the store.
    """

    schema: str
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.schema, str) or not self.schema:
            raise ReferenceValidationError(
                "ObjectReference schema must be a non-empty string"
            )
        if not isinstance(self.content_hash, str) or not is_content_hash(
            self.content_hash
        ):
            raise ReferenceValidationError(
                "ObjectReference content_hash must be a 64-character "
                f"lowercase hex SHA-256 hash, got {self.content_hash!r}"
            )

    @classmethod
    def for_record(cls, schema: str, record: Jsonable) -> ObjectReference:
        """Build the reference a record would resolve under."""
        return cls(schema=schema, content_hash=compute_content_hash(record))

    def verify_record(self, record: Jsonable) -> None:
        """Raise if ``record`` does not hash to this reference.

        Recomputes the Content Hash from scratch through dr-serialize and
        compares it to the declared hash; used on every verified read and on
        immutable put.
        """
        actual = compute_content_hash(record)
        if actual != self.content_hash:
            raise ContentHashMismatchError(
                expected=self.content_hash,
                actual=actual,
                schema=self.schema,
            )
