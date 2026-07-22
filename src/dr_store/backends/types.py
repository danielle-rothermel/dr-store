"""Result types for backend compare-and-set primitives."""

from __future__ import annotations

from dataclasses import dataclass


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
