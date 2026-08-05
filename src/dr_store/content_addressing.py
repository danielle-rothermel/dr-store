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
    return len(value) == CONTENT_HASH_LENGTH and all(
        char in _HEX_DIGITS for char in value
    )


def compute_content_hash(record: Jsonable) -> str:
    """Validate ``record`` and hash it with dr-serialize's canonical JSON."""
    return json_hash(validate_strict_json(record))


@dataclass(frozen=True, slots=True)
class ObjectReference:
    """A validated ``(schema, content_hash)`` reference."""

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
        return cls(schema=schema, content_hash=compute_content_hash(record))

    def verify_record(self, record: Jsonable) -> None:
        """Raise if ``record`` does not match this reference's content hash."""
        actual = compute_content_hash(record)
        if actual != self.content_hash:
            raise ContentHashMismatchError(
                expected=self.content_hash,
                actual=actual,
                schema=self.schema,
            )
