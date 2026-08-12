from __future__ import annotations

from typing import TYPE_CHECKING

from dr_store.core.reasons import ContentMismatchReason

if TYPE_CHECKING:
    from dr_store.content_addressing import ObjectReference


class StoreError(Exception):
    """Base for Object Store failures."""


class ReferenceValidationError(StoreError):
    pass


class ContentHashMismatchError(StoreError):
    """Covers hash mismatches and stored content that cannot be verified."""

    def __init__(
        self,
        *,
        expected: str,
        schema: str,
        reason: ContentMismatchReason,
        actual: str | None = None,
    ) -> None:
        if reason is ContentMismatchReason.HASH_MISMATCH:
            if actual is None:
                raise ValueError("actual is required for HASH_MISMATCH")
        elif actual is not None:
            raise ValueError(
                "actual must be None unless reason is HASH_MISMATCH"
            )
        self.expected = expected
        self.actual = actual
        self.schema = schema
        self.reason = reason
        if reason is ContentMismatchReason.HASH_MISMATCH:
            detail = f"observed {actual}"
        else:
            detail = reason.value
        super().__init__(
            f"content verification failed for schema {schema!r}: "
            f"expected {expected}, {detail}"
        )


class SchemaMismatchError(StoreError):
    def __init__(self, *, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"schema mismatch: reference declares {expected!r}, "
            f"stored content is {actual!r}"
        )


class ObjectNotFoundError(StoreError):
    def __init__(self, *, reference: ObjectReference) -> None:
        self.reference = reference
        super().__init__(
            f"no object for schema {reference.schema!r} "
            f"content_hash {reference.content_hash}"
        )


class ObjectConflictError(StoreError):
    """Preserves the occupied object row without exposing stored content."""

    def __init__(self, *, schema: str, content_hash: str) -> None:
        self.schema = schema
        self.content_hash = content_hash
        super().__init__(
            f"different content already stored at schema {schema!r} "
            f"content_hash {content_hash}"
        )


class BindingConflictError(StoreError):
    """Preserves and exposes the occupied binding as ``existing``."""

    def __init__(
        self,
        *,
        key: str,
        existing: ObjectReference,
        requested: ObjectReference,
    ) -> None:
        self.key = key
        self.existing = existing
        self.requested = requested
        super().__init__(
            f"key {key!r} already bound to "
            f"({existing.schema!r}, {existing.content_hash}); "
            f"refusing to rebind to "
            f"({requested.schema!r}, {requested.content_hash})"
        )


class SqliteRecordCacheClosedError(StoreError):
    """Raised when an operation is requested after closing begins."""


class SqliteRecordCacheCloseError(StoreError):
    """Raised when the managed SQLite cache cannot complete close."""
