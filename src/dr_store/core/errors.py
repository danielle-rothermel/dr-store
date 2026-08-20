"""Compatibility re-exports for the split core error modules."""

from dr_store.core.directory_errors import (
    AllocationError,
    DocumentDirectoryError,
    ManifestPublishError,
    ManifestReadError,
    SidecarVerificationError,
)
from dr_store.core.reasons import (
    ContentMismatchReason,
    RegularChildFailureReason,
)
from dr_store.core.store_errors import (
    BindingConflictError,
    ContentHashMismatchError,
    ObjectConflictError,
    ObjectNotFoundError,
    ReferenceValidationError,
    SchemaMismatchError,
    SqliteBackendClosedError,
    SqliteRecordCacheClosedError,
    SqliteRecordCacheCloseError,
    StoreError,
)
from dr_store.core.verified_read_errors import VerifiedRegularChildReadError

__all__ = [
    "AllocationError",
    "BindingConflictError",
    "ContentHashMismatchError",
    "ContentMismatchReason",
    "DocumentDirectoryError",
    "ManifestPublishError",
    "ManifestReadError",
    "ObjectConflictError",
    "ObjectNotFoundError",
    "ReferenceValidationError",
    "RegularChildFailureReason",
    "SchemaMismatchError",
    "SidecarVerificationError",
    "SqliteBackendClosedError",
    "SqliteRecordCacheCloseError",
    "SqliteRecordCacheClosedError",
    "StoreError",
    "VerifiedRegularChildReadError",
]
