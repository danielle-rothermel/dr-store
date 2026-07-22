"""The Object Store: immutable put, verified get, atomic binding.

:class:`ObjectStore` is the whole public contract. It owns every semantic
rule -- canonicalization (once, through dr-serialize), Content Hash
verification on write and on every read, idempotent replay, typed
conflicts, and the four-row atomic binding table -- and delegates only
atomic durability to a :class:`~dr_store.backends.base.Backend`.

The store is deliberately domain-neutral: the binding key is an opaque
caller-owned string and no Whetstone, Rollout, workflow, retry, or campaign
concept appears anywhere in this contract.
"""

from __future__ import annotations

import enum
import json
from typing import TYPE_CHECKING

from dr_serialize import Jsonable, canonical_json, validate_finite_json

from dr_store.errors import (
    BindingConflictError,
    ContentHashMismatchError,
    ObjectConflictError,
    ObjectNotFoundError,
    SchemaMismatchError,
)
from dr_store.references import ObjectReference

if TYPE_CHECKING:
    from dr_store.backends.base import Backend


class BindStatus(enum.Enum):
    """Non-conflicting outcome of :meth:`ObjectStore.bind`.

    ``BOUND`` -- an unbound key was atomically bound by this call.
    ``IDEMPOTENT`` -- the key was already bound to the same reference; the
    replay is success without a replacement write. A different-reference
    conflict is not a status: it raises :class:`BindingConflictError`.
    """

    BOUND = "bound"
    IDEMPOTENT = "idempotent"


class PutStatus(enum.Enum):
    """Non-conflicting outcome of :meth:`ObjectStore.put`.

    ``STORED`` -- an absent key atomically accepted the verified value.
    ``IDEMPOTENT`` -- the identical canonical value was already stored; the
    replay is success without a replacement write.
    """

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
        """Immutably store one complete canonical record.

        Returns the schema-typed :class:`ObjectReference` carrying the
        Content Hash of the complete canonical record plus a
        :class:`PutStatus`. An absent key accepts the verified value
        atomically (``STORED``); replay of the identical canonical value is
        idempotent success (``IDEMPOTENT``); different content colliding on
        the same content hash raises :class:`ObjectConflictError` and never
        overwrites the stored value.
        """
        validated = validate_finite_json(record)
        canonical = canonical_json(validated)
        reference = ObjectReference.for_record(schema, validated)
        outcome = self._backend.put_object(
            schema=schema,
            content_hash=reference.content_hash,
            canonical=canonical,
        )
        if outcome.inserted:
            return reference, PutStatus.STORED
        # The exact (schema, content_hash) key was already present: an
        # idempotent replay when the stored canonical bytes are identical,
        # otherwise a genuine content-hash collision at that typed key.
        if outcome.stored_canonical == canonical:
            return reference, PutStatus.IDEMPOTENT
        raise ObjectConflictError(
            schema=schema,
            content_hash=reference.content_hash,
        )

    def get(self, reference: ObjectReference) -> Jsonable:
        """Resolve a reference to its exact immutable record, verified.

        Recomputes and verifies the Content Hash and checks the declared
        schema on every read. Raises :class:`ObjectNotFoundError` when
        nothing resolves the reference, :class:`SchemaMismatchError` when
        stored content is filed under a different schema, and
        :class:`~dr_store.errors.ContentHashMismatchError` when stored
        content no longer hashes to its reference (corruption).
        """
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
        # Stored canonical bytes that no longer parse as JSON (bit rot,
        # truncation, a partial write) are corruption, not a caller error:
        # surface them as a typed content-hash mismatch so every corruption
        # mode fails through one contract exception, never a bare
        # json.JSONDecodeError.
        try:
            decoded = json.loads(canonical)
        except json.JSONDecodeError as exc:
            raise ContentHashMismatchError(
                expected=reference.content_hash,
                actual="<unparseable: stored content is not valid JSON>",
                schema=reference.schema,
            ) from exc
        record = validate_finite_json(decoded)
        reference.verify_record(record)
        return record

    def bind(
        self,
        key: str,
        reference: ObjectReference,
    ) -> BindStatus:
        """Atomically bind an opaque key to an Object Reference.

        The four-row binding contract, exactly:

        * **unbound + reference** -> bind, return ``BOUND``;
        * **bound to A + A** -> idempotent success, return ``IDEMPOTENT``;
        * **bound to A + B** -> raise :class:`BindingConflictError`,
          preserving A as the durable winner;
        * there is no overwrite/clear/rebind path.

        ``key`` is opaque to the store; the caller owns its meaning.
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
        """Return the reference bound to ``key``, or ``None`` if unbound.

        A read-only convenience over the binding table; it never mutates and
        exposes no overwrite path.
        """
        bound = self._backend.get_binding(key=key)
        if bound is None:
            return None
        return ObjectReference(schema=bound[0], content_hash=bound[1])
