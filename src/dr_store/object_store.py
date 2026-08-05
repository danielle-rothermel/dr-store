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

from dr_store.content_addressing import ObjectReference
from dr_store.core.errors import (
    BindingConflictError,
    ContentHashMismatchError,
    ObjectConflictError,
    ObjectNotFoundError,
    SchemaMismatchError,
)

if TYPE_CHECKING:
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

    def put(
        self,
        schema: str,
        record: Jsonable,
    ) -> tuple[ObjectReference, PutStatus]:
        """Store a record without overwriting its object row.

        Identical canonical text is idempotent; different text at the same
        schema and content-hash pair raises :class:`ObjectConflictError`.
        """
        validated = validate_strict_json(record)
        canonical = canonical_json(validated)
        reference = ObjectReference.for_record(schema, validated)
        outcome = self._backend.put_object(
            schema=schema,
            content_hash=reference.content_hash,
            canonical=canonical,
        )
        if outcome.inserted:
            return reference, PutStatus.STORED
        # Distinguish an idempotent replay from a hash collision.
        if outcome.stored_canonical == canonical:
            return reference, PutStatus.IDEMPOTENT
        raise ObjectConflictError(
            schema=schema,
            content_hash=reference.content_hash,
        )

    def get(self, reference: ObjectReference) -> Jsonable:
        """Read after verifying schema, hash, and canonical text."""
        stored = self._backend.get_object(
            schema=reference.schema,
            content_hash=reference.content_hash,
        )
        if stored is None:
            raise ObjectNotFoundError(reference=reference)
        stored_schema, canonical = stored
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
                actual="<stored content is not valid strict JSON>",
                schema=reference.schema,
            ) from exc
        try:
            reference.verify_record(record)
            verified_canonical = canonical_json(record)
        except JsonEncodeError as exc:
            raise ContentHashMismatchError(
                expected=reference.content_hash,
                actual="<stored content is outside the canonical profile>",
                schema=reference.schema,
            ) from exc
        # Stored text must equal its canonical re-encoding.
        if verified_canonical != canonical:
            raise ContentHashMismatchError(
                expected=reference.content_hash,
                actual="<stored content is not in canonical form>",
                schema=reference.schema,
            )
        return record

    def bind(
        self,
        key: str,
        reference: ObjectReference,
    ) -> BindStatus:
        """Bind an opaque key atomically without an overwrite path.

        Rebinding the same reference is idempotent; a different reference
        raises :class:`BindingConflictError` and preserves the existing one.
        """
        outcome = self._backend.bind(
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

    def resolve(self, key: str) -> ObjectReference | None:
        bound = self._backend.get_binding(key=key)
        if bound is None:
            return None
        return ObjectReference(schema=bound[0], content_hash=bound[1])
