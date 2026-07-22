"""dr-store: generic append-only content-addressed object store.

dr-store maps a typed :class:`ObjectReference` -- a declared record
``schema`` plus the full SHA-256 Content Hash of the complete canonical
persisted record -- to the exact immutable record value, and provides one
generic atomic key-to-reference binding. It owns three things and nothing
else:

* **Immutable put** -- an absent key atomically accepts a verified value;
  the identical canonical value replays idempotently; different content
  conflicts and never overwrites.
* **Verified get** -- every read recomputes and verifies the Content Hash
  and schema; missing, mismatched, or corrupt content fails with a typed
  error.
* **Atomic key-to-reference binding** -- an unbound key binds; the same
  reference replays idempotently; a different reference conflicts, keeping
  the durable winner; no overwrite path is exposed.

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
from dr_store.errors import (
    BindingConflictError,
    ContentHashMismatchError,
    ObjectConflictError,
    ObjectNotFoundError,
    ReferenceValidationError,
    SchemaMismatchError,
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
    "Backend",
    "BindOutcome",
    "BindStatus",
    "BindingConflictError",
    "ContentHashMismatchError",
    "MemoryBackend",
    "ObjectConflictError",
    "ObjectNotFoundError",
    "ObjectReference",
    "ObjectStore",
    "PutOutcome",
    "PutStatus",
    "ReferenceValidationError",
    "SchemaMismatchError",
    "SqliteBackend",
    "StoreError",
    "compute_content_hash",
    "is_content_hash",
]
