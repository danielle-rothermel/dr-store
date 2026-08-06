from __future__ import annotations

from typing import TYPE_CHECKING

from dr_store.content_addressing import compute_content_hash
from dr_store.core.errors import BindingConflictError, StoreError

if TYPE_CHECKING:
    from dr_serialize import Jsonable

    from dr_store.content_addressing import ObjectReference
    from dr_store.object_store import ObjectStore


def derive_cache_key(namespace: str, payload: Jsonable) -> str:
    """Derive a cache key from a namespace and one payload record.

    The payload is hashed through the same canonical JSON profile as a
    content hash, so equivalent payloads derive the same key.
    """
    return f"{namespace}:{compute_content_hash(payload)}"


class RecordCache:
    """Best-effort memoization facade over an :class:`ObjectStore`."""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    def get(self, key: str, *, schema: str) -> Jsonable | None:
        """Return the cached record, or ``None`` for any storage-level miss.

        An unbound key, a different schema, missing content, and failed
        verification are all misses; no storage fault reaches the caller.
        """
        reference = self._store.resolve(key)
        if reference is None or reference.schema != schema:
            return None
        try:
            record = self._store.get(reference)
        except StoreError:
            return None
        return record

    def put(
        self,
        key: str,
        schema: str,
        record: Jsonable,
    ) -> ObjectReference:
        """Store ``record`` and bind ``key`` to it, keeping the first winner.

        A conflicting binding returns the existing reference instead of
        replacing it; record validation failures are caller bugs and raise.
        """
        reference, _ = self._store.put(schema, record)
        try:
            self._store.bind(key, reference)
        except BindingConflictError as conflict:
            return conflict.existing
        return reference
