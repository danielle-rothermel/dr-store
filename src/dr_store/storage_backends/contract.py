"""Backend-neutral storage contract for the Object Store.

A backend provides two atomic compare-and-set primitives -- one for the
append-only object table, one for the append-only binding table -- plus point
reads. All contract semantics (hash verification, idempotent replay, conflict
typing) live in :mod:`dr_store.object_store`; a backend only guarantees
atomicity and durability of the two primitives.

The record value crossing this boundary is the *canonical JSON text* of the
complete persisted record: dr-store hashes and canonicalizes exactly once,
above the backend, so every backend stores identical bytes and no backend can
introduce a second canonicalization dialect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PutOutcome:
    """Outcome of :meth:`Backend.put_object`.

    ``inserted`` is ``True`` when this call created the row. When ``False``,
    the key was already present and ``stored_schema``/``stored_canonical``
    carry the untouched existing row for the caller to compare.
    """

    inserted: bool
    stored_schema: str
    stored_canonical: str


@dataclass(frozen=True, slots=True)
class BindOutcome:
    """Outcome of :meth:`Backend.bind`.

    ``bound`` is ``True`` when this call created the binding. When ``False``,
    the key was already bound and ``existing_schema``/``existing_content_hash``
    carry the untouched durable winner.
    """

    bound: bool
    existing_schema: str
    existing_content_hash: str


class Backend(Protocol):
    """Atomic, durable primitives for the two append-only tables."""

    def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        """Atomically insert one object row if the key is absent.

        The key is ``(schema, content_hash)``. If absent, insert and report
        ``inserted``. If already present, report ``existed`` and return the
        stored canonical text and its stored schema unchanged -- never
        overwrite. The caller decides whether an ``existed`` outcome is an
        idempotent replay or a conflict.
        """
        ...

    def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        """Return ``(stored_schema, canonical)`` for a reference key.

        Prefer the exact ``(schema, content_hash)`` row. When no such row
        exists but the same content is filed under a *different* schema,
        return that other row's ``(stored_schema, canonical)`` so the store
        can raise a schema mismatch rather than presenting as a missing
        object. Return ``None`` only when nothing is stored at
        ``content_hash`` under any schema.
        """
        ...

    def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        """Atomically bind ``key`` to ``(schema, content_hash)`` if unbound.

        If ``key`` is unbound, create the binding and report ``bound``. If
        already bound, report ``existed`` and return the existing
        ``(schema, content_hash)`` unchanged -- never overwrite. The caller
        decides whether an ``existed`` outcome is idempotent success or a
        different-reference conflict.
        """
        ...

    def get_binding(self, *, key: str) -> tuple[str, str] | None:
        """Return the ``(schema, content_hash)`` bound to ``key``, or None."""
        ...
