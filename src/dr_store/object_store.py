from __future__ import annotations

import enum
import json
from typing import TYPE_CHECKING

from dr_serialize import (
    Jsonable,
    JsonEncodeError,
    StrictJsonError,
    canonical_json,
    validate_strict_json,
)

from dr_store.content_addressing import (
    ObjectReference,
    _hash_canonical,
    _prepare_record,
    _validate_binding_key,
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
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dr_store.storage_backends.contract import Backend


class BindStatus(enum.Enum):
    BOUND = "bound"
    IDEMPOTENT = "idempotent"


class PutStatus(enum.Enum):
    STORED = "stored"
    IDEMPOTENT = "idempotent"


class ObjectStore:
    """Append-only content-addressed store over a pluggable backend."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def put(
        self,
        schema: str,
        record: Jsonable,
    ) -> tuple[ObjectReference, PutStatus]:
        """Store a record without overwriting its object row.

        Identical canonical text is idempotent; different text at the same
        schema and content-hash pair raises :class:`ObjectConflictError`.
        """
        prepared = _prepare_record(record)
        reference = ObjectReference(
            schema=schema,
            content_hash=prepared.content_hash,
        )
        outcome = await self._backend.put_object(
            schema=schema,
            content_hash=reference.content_hash,
            canonical=prepared.canonical,
        )
        if outcome.inserted:
            return reference, PutStatus.STORED
        # Distinguish an idempotent replay from a hash collision.
        if outcome.stored_canonical == prepared.canonical:
            return reference, PutStatus.IDEMPOTENT
        raise ObjectConflictError(
            schema=schema,
            content_hash=reference.content_hash,
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
        return self._verify_stored_record(
            reference=reference,
            stored_schema=stored_schema,
            canonical=canonical,
        )

    def _verify_stored_record(
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

    async def _get_bound_objects(
        self,
        keys: tuple[str, ...],
    ) -> Mapping[str, BoundObjectRow]:
        for key in keys:
            _validate_binding_key(key)
        return await self._backend.get_bound_objects(keys=keys)

    async def _put_bound_records(
        self,
        entries: Mapping[str, tuple[str, Jsonable]],
    ) -> dict[str, ObjectReference]:
        writes: list[BoundObjectWrite] = []
        for key, (schema, record) in entries.items():
            _validate_binding_key(key)
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

    async def bind(
        self,
        key: str,
        reference: ObjectReference,
    ) -> BindStatus:
        """Bind an opaque key atomically without an overwrite path.

        Rebinding the same reference is idempotent; a different reference
        raises :class:`BindingConflictError` and preserves the existing one.
        """
        _validate_binding_key(key)
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

    async def resolve(self, key: str) -> ObjectReference | None:
        _validate_binding_key(key)
        bound = await self._backend.get_binding(key=key)
        if bound is None:
            return None
        return ObjectReference(schema=bound[0], content_hash=bound[1])
