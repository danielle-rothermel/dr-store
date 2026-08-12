from __future__ import annotations

import enum
import json
from collections.abc import Iterable, Mapping  # noqa: TC003
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dr_serialize import (
    Jsonable,
    JsonEncodeError,
    StrictJsonError,
    canonical_json,
    validate_strict_json,
)
from sqlalchemy.engine import Connection  # noqa: TC002

from dr_store.content_addressing import (
    ObjectReference,
    _hash_canonical,
    _prepare_record,
    _PreparedRecord,
    validate_binding_key,
    validate_reference_schema,
)
from dr_store.core.errors import (
    BindingConflictError,
    ContentHashMismatchError,
    ContentMismatchReason,
    ObjectConflictError,
    ObjectNotFoundError,
    SchemaMismatchError,
)
from dr_store.storage_backends.contract import (
    BoundObjectRow,
    BoundObjectWrite,
    PutOutcome,
)
from dr_store.storage_backends.postgresql import PostgresBackend

if TYPE_CHECKING:
    from dr_store.storage_backends.contract import Backend


class BindStatus(enum.Enum):
    BOUND = "bound"
    IDEMPOTENT = "idempotent"


class PutStatus(enum.Enum):
    STORED = "stored"
    IDEMPOTENT = "idempotent"


@dataclass(frozen=True, slots=True)
class StoreHit:
    """One verified bound record, including a strict-JSON ``null`` record."""

    record: Jsonable


class ObjectStore:
    """Append-only content-addressed store over a pluggable backend."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    def _require_postgres_backend(self) -> PostgresBackend:
        if not isinstance(self._backend, PostgresBackend):
            raise TypeError(
                "enlisted operations require a PostgresBackend instance"
            )
        return self._backend

    def _put_from_prepared(
        self,
        *,
        schema: str,
        prepared: _PreparedRecord,
        put_outcome: PutOutcome,
    ) -> tuple[ObjectReference, PutStatus]:
        reference = ObjectReference(
            schema=schema,
            content_hash=prepared.content_hash,
        )
        if put_outcome.inserted:
            return reference, PutStatus.STORED
        if put_outcome.stored_canonical == prepared.canonical:
            return reference, PutStatus.IDEMPOTENT
        raise ObjectConflictError(
            schema=schema,
            content_hash=reference.content_hash,
        )

    async def put(
        self,
        schema: str,
        record: Jsonable,
    ) -> tuple[ObjectReference, PutStatus]:
        """Store a record without overwriting its object row.

        Identical canonical text is idempotent; different text at the same
        schema and content-hash pair raises :class:`ObjectConflictError`.
        """
        validate_reference_schema(schema)
        prepared = _prepare_record(record)
        outcome = await self._backend.put_object(
            schema=schema,
            content_hash=prepared.content_hash,
            canonical=prepared.canonical,
        )
        return self._put_from_prepared(
            schema=schema,
            prepared=prepared,
            put_outcome=outcome,
        )

    def put_enlisted(
        self,
        connection: Connection,
        schema: str,
        record: Jsonable,
    ) -> tuple[ObjectReference, PutStatus]:
        """Store a record on a caller-owned sync transaction connection."""
        validate_reference_schema(schema)
        prepared = _prepare_record(record)
        backend = self._require_postgres_backend()
        outcome = backend.put_object_enlisted(
            schema=schema,
            content_hash=prepared.content_hash,
            canonical=prepared.canonical,
            connection=connection,
        )
        return self._put_from_prepared(
            schema=schema,
            prepared=prepared,
            put_outcome=outcome,
        )

    async def get(self, reference: ObjectReference) -> Jsonable:
        """Read after verifying schema, hash, and canonical text."""
        stored = await self._backend.get_object(
            schema=reference.schema,
            content_hash=reference.content_hash,
        )
        if stored is None:
            raise ObjectNotFoundError(reference=reference)
        stored_schema, canonical = stored
        return self.verify_stored_record(
            reference=reference,
            stored_schema=stored_schema,
            canonical=canonical,
        )

    def verify_stored_record(
        self,
        *,
        reference: ObjectReference,
        stored_schema: str,
        canonical: str,
    ) -> Jsonable:
        if stored_schema != reference.schema:
            raise SchemaMismatchError(
                expected=reference.schema,
                actual=stored_schema,
            )
        # Stored parse failures are corruption, not caller validation errors.
        try:
            record = validate_strict_json(json.loads(canonical))
        except (ValueError, RecursionError, StrictJsonError) as exc:
            raise ContentHashMismatchError(
                expected=reference.content_hash,
                schema=reference.schema,
                reason=ContentMismatchReason.INVALID_JSON,
            ) from exc
        try:
            verified_canonical = canonical_json(record)
        except JsonEncodeError as exc:
            raise ContentHashMismatchError(
                expected=reference.content_hash,
                schema=reference.schema,
                reason=ContentMismatchReason.NON_CANONICAL_PROFILE,
            ) from exc
        actual_hash = _hash_canonical(verified_canonical)
        if actual_hash != reference.content_hash:
            raise ContentHashMismatchError(
                expected=reference.content_hash,
                actual=actual_hash,
                schema=reference.schema,
                reason=ContentMismatchReason.HASH_MISMATCH,
            )
        # Stored text must equal its canonical re-encoding.
        if verified_canonical != canonical:
            raise ContentHashMismatchError(
                expected=reference.content_hash,
                schema=reference.schema,
                reason=ContentMismatchReason.NON_CANONICAL_FORM,
            )
        return record

    async def get_many(
        self,
        keys: Iterable[str],
        *,
        schema: str,
    ) -> dict[str, StoreHit | None]:
        """Return one verified hit or unbound ``None`` per distinct key.

        Invalid requested schemas, schema mismatches, missing referenced
        objects, and unverifiable stored content raise typed errors.
        """
        validated_schema = validate_reference_schema(schema)
        distinct = tuple(dict.fromkeys(keys))
        rows = await self.get_bound_objects(distinct)
        results: dict[str, StoreHit | None] = {}
        for key in distinct:
            row = rows.get(key)
            if row is None:
                results[key] = None
                continue
            reference = ObjectReference(
                schema=row.binding_schema,
                content_hash=row.binding_content_hash,
            )
            if reference.schema != validated_schema:
                raise SchemaMismatchError(
                    expected=validated_schema,
                    actual=reference.schema,
                )
            if row.canonical is None:
                raise ObjectNotFoundError(reference=reference)
            results[key] = StoreHit(
                record=self.verify_stored_record(
                    reference=reference,
                    stored_schema=row.binding_schema,
                    canonical=row.canonical,
                )
            )
        return results

    async def put_many(
        self,
        entries: Mapping[str, tuple[str, Jsonable]],
    ) -> dict[str, ObjectReference]:
        """Store records and return the first binding winner for each key."""
        writes: list[BoundObjectWrite] = []
        for key, (schema, record) in entries.items():
            validate_binding_key(key)
            prepared = _prepare_record(record)
            reference = ObjectReference(
                schema=schema,
                content_hash=prepared.content_hash,
            )
            writes.append(
                BoundObjectWrite(
                    key=key,
                    schema=reference.schema,
                    content_hash=reference.content_hash,
                    canonical=prepared.canonical,
                )
            )

        outcomes = await self._backend.put_bound_objects(entries=tuple(writes))
        return {
            key: ObjectReference(
                schema=outcome.existing_schema,
                content_hash=outcome.existing_content_hash,
            )
            for key, outcome in outcomes.items()
        }

    def put_many_enlisted(
        self,
        connection: Connection,
        entries: Mapping[str, tuple[str, Jsonable]],
    ) -> dict[str, ObjectReference]:
        """Store records on a caller-owned sync transaction connection."""
        writes: list[BoundObjectWrite] = []
        for key, (schema, record) in entries.items():
            validate_binding_key(key)
            prepared = _prepare_record(record)
            reference = ObjectReference(
                schema=schema,
                content_hash=prepared.content_hash,
            )
            writes.append(
                BoundObjectWrite(
                    key=key,
                    schema=reference.schema,
                    content_hash=reference.content_hash,
                    canonical=prepared.canonical,
                )
            )

        backend = self._require_postgres_backend()
        outcomes = backend.put_bound_objects_enlisted(
            entries=tuple(writes),
            connection=connection,
        )
        return {
            key: ObjectReference(
                schema=outcome.existing_schema,
                content_hash=outcome.existing_content_hash,
            )
            for key, outcome in outcomes.items()
        }

    async def get_bound_objects(
        self,
        keys: Iterable[str],
    ) -> Mapping[str, BoundObjectRow]:
        """Return joined binding/object rows without verification."""
        distinct = tuple(dict.fromkeys(keys))
        for key in distinct:
            validate_binding_key(key)
        return await self._backend.get_bound_objects(keys=distinct)

    def get_bound_objects_enlisted(
        self,
        connection: Connection,
        keys: Iterable[str],
    ) -> Mapping[str, BoundObjectRow]:
        """Return joined binding/object rows on a caller-owned connection."""
        distinct = tuple(dict.fromkeys(keys))
        for key in distinct:
            validate_binding_key(key)
        backend = self._require_postgres_backend()
        return backend.get_bound_objects_enlisted(
            keys=distinct,
            connection=connection,
        )

    async def bind(
        self,
        key: str,
        reference: ObjectReference,
    ) -> BindStatus:
        """Bind an opaque key atomically without an overwrite path.

        Rebinding the same reference is idempotent; a different reference
        raises :class:`BindingConflictError` and preserves the existing one.
        """
        validate_binding_key(key)
        outcome = await self._backend.bind(
            key=key,
            schema=reference.schema,
            content_hash=reference.content_hash,
        )
        if outcome.bound:
            return BindStatus.BOUND
        existing = ObjectReference(
            schema=outcome.existing_schema,
            content_hash=outcome.existing_content_hash,
        )
        if existing == reference:
            return BindStatus.IDEMPOTENT
        raise BindingConflictError(
            key=key,
            existing=existing,
            requested=reference,
        )

    def bind_enlisted(
        self,
        connection: Connection,
        key: str,
        reference: ObjectReference,
    ) -> BindStatus:
        """Bind an opaque key on a caller-owned sync transaction connection."""
        validate_binding_key(key)
        backend = self._require_postgres_backend()
        outcome = backend.bind_enlisted(
            key=key,
            schema=reference.schema,
            content_hash=reference.content_hash,
            connection=connection,
        )
        if outcome.bound:
            return BindStatus.BOUND
        existing = ObjectReference(
            schema=outcome.existing_schema,
            content_hash=outcome.existing_content_hash,
        )
        if existing == reference:
            return BindStatus.IDEMPOTENT
        raise BindingConflictError(
            key=key,
            existing=existing,
            requested=reference,
        )

    async def resolve(self, key: str) -> ObjectReference | None:
        validate_binding_key(key)
        bound = await self._backend.get_binding(key=key)
        if bound is None:
            return None
        return ObjectReference(schema=bound[0], content_hash=bound[1])
