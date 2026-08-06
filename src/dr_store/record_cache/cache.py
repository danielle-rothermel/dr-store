from __future__ import annotations

from collections.abc import (  # noqa: TC003 - public hints resolve at runtime.
    Iterable,
    Mapping,
)
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dr_serialize import Jsonable

from dr_store.content_addressing import (
    ObjectReference,
    _validate_reference_schema,
    compute_content_hash,
)
from dr_store.core.errors import (
    ContentHashMismatchError,
    ObjectNotFoundError,
    ReferenceValidationError,
    SchemaMismatchError,
)

if TYPE_CHECKING:
    from dr_store.object_store import ObjectStore


def derive_cache_key(namespace: str, payload: Jsonable) -> str:
    """Derive a key through the Object Store's canonical content hash."""
    if not isinstance(namespace, str):
        raise TypeError("namespace must be a string")
    return f"{namespace}:{compute_content_hash(payload)}"


@dataclass(frozen=True, slots=True)
class CacheHit:
    """A cached record, including a strict-JSON ``null`` record."""

    record: Jsonable


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One schema-qualified record proposed for a cache key."""

    schema: str
    record: Jsonable


class RecordCache:
    """Best-effort memoization facade over an :class:`ObjectStore`."""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    def get(self, key: str, *, schema: str) -> CacheHit | None:
        """Return a hit or a miss for absent or unverifiable stored data.

        Invalid requested schemas and operational backend failures raise.
        """
        return self._get_many((key,), schema=schema)[key]

    def get_many(
        self,
        keys: Iterable[str],
        *,
        schema: str,
    ) -> dict[str, CacheHit | None]:
        """Return one hit or miss for every distinct requested key."""
        return self._get_many(tuple(dict.fromkeys(keys)), schema=schema)

    def _get_many(
        self,
        keys: tuple[str, ...],
        *,
        schema: str,
    ) -> dict[str, CacheHit | None]:
        validated_schema = _validate_reference_schema(schema)
        rows = self._store._get_bound_objects(keys)
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
                if row.object_schema is None or row.canonical is None:
                    results[key] = None
                    continue
                record = self._store._verify_stored_record(
                    reference=reference,
                    stored_schema=row.object_schema,
                    canonical=row.canonical,
                )
            except (
                ContentHashMismatchError,
                ObjectNotFoundError,
                ReferenceValidationError,
                SchemaMismatchError,
            ):
                results[key] = None
            else:
                results[key] = CacheHit(record=record)
        return results

    def put(
        self,
        key: str,
        schema: str,
        record: Jsonable,
    ) -> ObjectReference:
        """Store a record and bind its key, keeping the first winner."""
        return self._put_many({key: CacheEntry(schema=schema, record=record)})[
            key
        ]

    def put_many(
        self,
        entries: Mapping[str, CacheEntry],
    ) -> dict[str, ObjectReference]:
        """Store records and return the first binding winner for each key."""
        return self._put_many(entries)

    def _put_many(
        self,
        entries: Mapping[str, CacheEntry],
    ) -> dict[str, ObjectReference]:
        return self._store._put_bound_records(
            {
                key: (entry.schema, entry.record)
                for key, entry in entries.items()
            }
        )
