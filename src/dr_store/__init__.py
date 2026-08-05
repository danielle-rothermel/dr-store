"""dr-store: generic append-only content-addressed object store.

dr-store maps a typed :class:`ObjectReference` -- a declared record
``schema`` plus the full SHA-256 Content Hash of the complete canonical
persisted record -- to the immutable canonical (JSON-equivalent) record
value, and provides one generic atomic key-to-reference binding. It owns
three things and nothing else:

* **Immutable put** -- an absent key atomically accepts a verified value;
  the identical canonical value replays idempotently; different content
  conflicts and never overwrites.
* **Verified get** -- every read recomputes and verifies the Content Hash
  and schema; missing, mismatched, or corrupt content fails with a typed
  error.
* **Atomic key-to-reference binding** -- an unbound key binds; the same
  reference replays idempotently; a different reference conflicts, keeping
  the durable winner; no overwrite path is exposed.

Alongside the Object Store, dr-store provides the **Document Directory**: a
durable crash-consistent directory holding one atomically-replaced
canonical-JSON Manifest plus streamed binary Sidecars, for artifacts too
large or too incremental for a single immutable record.

Canonicalization and hashing come entirely from ``dr-serialize``; dr-store
invents no second canonicalization dialect, and a Content Hash is not an
Identity Hash. The contract carries no Whetstone, Rollout, workflow, retry,
or campaign vocabulary.
"""

from __future__ import annotations

from dr_store.backends import (
    Backend,
    BindOutcome,
    MemoryBackend,
    PutOutcome,
    SqliteBackend,
)
from dr_store.docdir import (
    DocumentDirectory,
    SidecarSummary,
    SidecarWriter,
)
from dr_store.errors import (
    AllocationError,
    BindingConflictError,
    ContentHashMismatchError,
    DocumentDirectoryError,
    ManifestPublishError,
    ManifestReadError,
    ObjectConflictError,
    ObjectNotFoundError,
    ReferenceValidationError,
    SchemaMismatchError,
    SidecarVerificationError,
    StoreError,
)
from dr_store.references import (
    CONTENT_HASH_LENGTH,
    ObjectReference,
    compute_content_hash,
    is_content_hash,
)
from dr_store.store import (
    BindStatus,
    ObjectStore,
    PutStatus,
)

__all__ = [
    "CONTENT_HASH_LENGTH",
    "AllocationError",
    "Backend",
    "BindOutcome",
    "BindStatus",
    "BindingConflictError",
    "ContentHashMismatchError",
    "DocumentDirectory",
    "DocumentDirectoryError",
    "ManifestPublishError",
    "ManifestReadError",
    "MemoryBackend",
    "ObjectConflictError",
    "ObjectNotFoundError",
    "ObjectReference",
    "ObjectStore",
    "PutOutcome",
    "PutStatus",
    "ReferenceValidationError",
    "SchemaMismatchError",
    "SidecarSummary",
    "SidecarVerificationError",
    "SidecarWriter",
    "SqliteBackend",
    "StoreError",
    "compute_content_hash",
    "is_content_hash",
]
