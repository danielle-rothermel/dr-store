from __future__ import annotations

import logging
from collections.abc import (  # noqa: TC003 - public hints resolve at runtime.
    Iterable,
    Mapping,
)
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dr_serialize import Jsonable

from dr_store.content_addressing import (
    ObjectReference,
    compute_content_hash,
    validate_binding_key,
    validate_reference_schema,
)
from dr_store.core.errors import (
    ContentHashMismatchError,
    ObjectNotFoundError,
    ReferenceValidationError,
    SchemaMismatchError,
)

if TYPE_CHECKING:
    from dr_store.object_store import ObjectStore

_LOGGER = logging.getLogger(__name__)


def derive_cache_key(namespace: str, payload: Jsonable) -> str:
    """Derive a key through the Object Store's canonical content hash."""
    if not isinstance(namespace, str):
        raise TypeError("namespace must be a string")
    key = f"{namespace}:{compute_content_hash(payload)}"
    validate_binding_key(key)
    return key


@dataclass(frozen=True, slots=True)
class CacheHit:
    """A cached record, including a strict-JSON ``null`` record."""

    record: Jsonable


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One schema-qualified record proposed for a cache key."""

    schema: str
    record: Jsonable


@dataclass(frozen=True, slots=True)
class RecordCacheStats:
    """Observed record-cache read outcomes."""

    corruption_count: int


class RecordCache:
    """Best-effort memoization facade over an :class:`ObjectStore`."""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store
        self._corruption_count = 0

    @property
    def stats(self) -> RecordCacheStats:
        """Return observed corruption outcomes from best-effort reads."""
        return RecordCacheStats(corruption_count=self._corruption_count)

    async def get(self, key: str, *, schema: str) -> CacheHit | None:
        """Return a hit or a miss for absent or unverifiable stored data.

        Invalid requested schemas and operational backend failures raise.
        """
        return (await self._get_many((key,), schema=schema))[key]

    async def get_many(
        self,
        keys: Iterable[str],
        *,
        schema: str,
    ) -> dict[str, CacheHit | None]:
        """Return one hit or miss for every distinct requested key."""
        return await self._get_many(tuple(dict.fromkeys(keys)), schema=schema)

    async def _get_many(
        self,
        keys: tuple[str, ...],
        *,
        schema: str,
    ) -> dict[str, CacheHit | None]:
        validated_schema = validate_reference_schema(schema)
        for key in keys:
            validate_binding_key(key)
        rows = await self._store.get_bound_objects(keys)
        results: dict[str, CacheHit | None] = {}
        for key in keys:
            row = rows.get(key)
            if row is None:
                results[key] = None
                continue
            try:
                reference = ObjectReference(
                    schema=row.binding_schema,
                    content_hash=row.binding_content_hash,
                )
                if reference.schema != validated_schema:
                    results[key] = None
                    continue
                if row.canonical is None:
                    results[key] = None
                    continue
                record = self._store.verify_stored_record(
                    reference=reference,
                    stored_schema=row.binding_schema,
                    canonical=row.canonical,
                )
            except (
                ContentHashMismatchError,
                ObjectNotFoundError,
                ReferenceValidationError,
                SchemaMismatchError,
            ):
                self._corruption_count += 1
                _LOGGER.warning(
                    "record cache corruption for key %r",
                    key,
                )
                results[key] = None
            else:
                results[key] = CacheHit(record=record)
        return results

    async def put(
        self,
        key: str,
        schema: str,
        record: Jsonable,
    ) -> ObjectReference:
        """Store a record and bind its key, keeping the first winner."""
        return (
            await self._put_many(
                {key: CacheEntry(schema=schema, record=record)}
            )
        )[key]

    async def put_many(
        self,
        entries: Mapping[str, CacheEntry],
    ) -> dict[str, ObjectReference]:
        """Store records and return the first binding winner for each key."""
        return await self._put_many(entries)

    async def _put_many(
        self,
        entries: Mapping[str, CacheEntry],
    ) -> dict[str, ObjectReference]:
        return await self._store.put_many(
            {
                key: (entry.schema, entry.record)
                for key, entry in entries.items()
            }
        )
