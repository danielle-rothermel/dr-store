from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PutOutcome:
    """Atomic put result; ``inserted=False`` carries the stored row."""

    inserted: bool
    stored_schema: str
    stored_canonical: str


@dataclass(frozen=True, slots=True)
class BindOutcome:
    """Atomic bind result; ``bound=False`` carries the existing binding."""

    bound: bool
    existing_schema: str
    existing_content_hash: str


@dataclass(frozen=True, slots=True)
class BoundObjectRow:
    """One binding and its exact object row, when that row exists."""

    binding_schema: str
    binding_content_hash: str
    object_schema: str | None
    canonical: str | None


@dataclass(frozen=True, slots=True)
class BoundObjectWrite:
    """One prepared object and the key to bind to it."""

    key: str
    schema: str
    content_hash: str
    canonical: str


class Backend(Protocol):
    """Atomic object and binding operations."""

    def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        """Insert ``(schema, content_hash)`` without overwriting.

        ``inserted=False`` returns the untouched row for conflict handling.
        ``canonical`` is canonical JSON text; a backend defines no other
        canonicalization dialect.
        """
        ...

    def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        """Look up exact schema first, then any row with the same content hash.

        Return ``None`` only when no schema has that content hash.
        """
        ...

    def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        """Bind ``key`` without overwriting an existing binding.

        ``bound=False`` returns the untouched binding for conflict handling.
        """
        ...

    def get_binding(self, *, key: str) -> tuple[str, str] | None: ...

    def get_bound_objects(
        self,
        *,
        keys: tuple[str, ...],
    ) -> dict[str, BoundObjectRow]:
        """Return bound rows for the requested exact keys."""
        ...

    def put_bound_objects(
        self,
        *,
        entries: tuple[BoundObjectWrite, ...],
    ) -> dict[str, BindOutcome]:
        """Atomically store prepared objects and bind their keys."""
        ...
