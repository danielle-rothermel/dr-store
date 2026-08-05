from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dr_store.content_addressing import ObjectReference


class StoreError(Exception):
    """Base for Object Store failures."""


class ReferenceValidationError(StoreError):
    pass


class ContentHashMismatchError(StoreError):
    """Covers hash mismatches and stored content that cannot be hashed.

    ``actual`` is the observed hash or a diagnostic sentinel.
    """

    def __init__(
        self,
        *,
        expected: str,
        actual: str,
        schema: str,
    ) -> None:
        self.expected = expected
        self.actual = actual
        self.schema = schema
        super().__init__(
            f"content hash mismatch for schema {schema!r}: "
            f"expected {expected}, observed {actual}"
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


class DocumentDirectoryError(Exception):
    """Base for every Document Directory failure."""


class AllocationError(DocumentDirectoryError):
    """Covers Document Directory allocation, name, and cap faults.

    Also covers Sidecar open, write, and finalize failures.
    """


class ManifestPublishError(DocumentDirectoryError):
    pass


class ManifestReadError(DocumentDirectoryError):
    pass


class SidecarVerificationError(DocumentDirectoryError):
    pass
