from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from dr_store.document_file.errors import (
    PublicationStage,
    ReadReason,
    ReadStage,
    ReplacementState,
)

if TYPE_CHECKING:
    from pathlib import Path

    from dr_store.content_addressing import ObjectReference


class ContentMismatchReason(StrEnum):
    HASH_MISMATCH = "hash_mismatch"
    INVALID_JSON = "invalid_json"
    NON_CANONICAL_PROFILE = "non_canonical_profile"
    NON_CANONICAL_FORM = "non_canonical_form"


class SidecarVerificationReason(StrEnum):
    MISSING = "missing"
    NOT_REGULAR = "not_regular"
    MISMATCH = "mismatch"
    BOUNDS_EXCEEDED = "bounds_exceeded"
    UNSUPPORTED_PLATFORM = "unsupported_platform"


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


class DocumentDirectoryError(Exception):
    """Base for every Document Directory failure."""


class AllocationError(DocumentDirectoryError):
    """Covers Document Directory allocation, name, and cap faults.

    Also covers Sidecar open, write, and finalize failures.
    """


class ManifestPublishError(DocumentDirectoryError):
    """A failed manifest publication with explicit phase and state."""

    def __init__(
        self,
        path: Path,
        stage: PublicationStage,
        *,
        replacement_state: ReplacementState,
    ) -> None:
        self.path = path
        self.stage = stage
        self.replacement_state = replacement_state
        if replacement_state is ReplacementState.NOT_REPLACED:
            state = "without replacing the target"
        elif replacement_state is ReplacementState.REPLACED:
            state = "after replacing the target"
        else:
            state = "with an unknown replacement outcome"
        super().__init__(
            f"could not publish manifest {str(path)!r} at "
            f"{stage.value!r} {state}"
        )


class ManifestReadError(DocumentDirectoryError):
    """A failed bounded, strict, canonical manifest read."""

    def __init__(
        self,
        path: Path,
        stage: ReadStage,
        *,
        reason: ReadReason,
    ) -> None:
        self.path = path
        self.stage = stage
        self.reason = reason
        super().__init__(
            f"could not read manifest {str(path)!r} at "
            f"{stage.value!r} with reason {reason.value!r}"
        )


class SidecarVerificationError(DocumentDirectoryError):
    def __init__(
        self,
        path: Path,
        reason: SidecarVerificationReason,
    ) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"sidecar verification failed for {str(path)!r}: {reason.value!r}"
        )


class VerifiedRegularChildReadError(Exception):
    """Failed to read or verify one pinned regular direct child."""
