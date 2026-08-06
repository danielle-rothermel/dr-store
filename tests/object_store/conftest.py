from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dr_store import (
    BindOutcome,
    BoundObjectRow,
    BoundObjectWrite,
    ObjectConflictError,
    PutOutcome,
)


@dataclass(slots=True)
class ControlledBackend:
    object_row: tuple[str, str, str] | None = None
    object_rows: dict[tuple[str, str], str] = field(default_factory=dict)
    bindings: dict[str, tuple[str, str]] = field(default_factory=dict)
    put_calls: int = 0

    def set_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> None:
        self.object_row = (schema, content_hash, canonical)
        self.object_rows[(schema, content_hash)] = canonical

    def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        self.put_calls += 1
        existing = self.object_rows.get((schema, content_hash))
        if existing is None:
            self.set_object(
                schema=schema,
                content_hash=content_hash,
                canonical=canonical,
            )
            return PutOutcome(
                inserted=True,
                stored_schema=schema,
                stored_canonical=canonical,
            )
        return PutOutcome(
            inserted=False,
            stored_schema=schema,
            stored_canonical=existing,
        )

    def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        exact = self.object_rows.get((schema, content_hash))
        if exact is not None:
            return (schema, exact)
        for (
            stored_schema,
            stored_hash,
        ), canonical in self.object_rows.items():
            if stored_hash == content_hash:
                return (stored_schema, canonical)
        return None

    def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        existing = self.bindings.get(key)
        if existing is None:
            self.bindings[key] = (schema, content_hash)
            return BindOutcome(
                bound=True,
                existing_schema=schema,
                existing_content_hash=content_hash,
            )
        return BindOutcome(
            bound=False,
            existing_schema=existing[0],
            existing_content_hash=existing[1],
        )

    def get_binding(self, *, key: str) -> tuple[str, str] | None:
        return self.bindings.get(key)

    def get_bound_objects(
        self,
        *,
        keys: tuple[str, ...],
    ) -> dict[str, BoundObjectRow]:
        rows: dict[str, BoundObjectRow] = {}
        for key in keys:
            binding = self.bindings.get(key)
            if binding is None:
                continue
            schema, content_hash = binding
            canonical = self.object_rows.get((schema, content_hash))
            rows[key] = BoundObjectRow(
                binding_schema=schema,
                binding_content_hash=content_hash,
                object_schema=schema if canonical is not None else None,
                canonical=canonical,
            )
        return rows

    def put_bound_objects(
        self,
        *,
        entries: tuple[BoundObjectWrite, ...],
    ) -> dict[str, BindOutcome]:
        self.put_calls += 1
        proposed: dict[tuple[str, str], str] = {}
        for entry in entries:
            object_key = (entry.schema, entry.content_hash)
            canonical = proposed.setdefault(object_key, entry.canonical)
            stored = self.object_rows.get(object_key)
            if canonical != entry.canonical or (
                stored is not None and stored != entry.canonical
            ):
                raise ObjectConflictError(
                    schema=entry.schema,
                    content_hash=entry.content_hash,
                )

        for (schema, content_hash), canonical in proposed.items():
            self.set_object(
                schema=schema,
                content_hash=content_hash,
                canonical=canonical,
            )

        outcomes: dict[str, BindOutcome] = {}
        for entry in entries:
            if entry.key in outcomes:
                continue
            existing = self.bindings.get(entry.key)
            if existing is None:
                existing = (entry.schema, entry.content_hash)
                self.bindings[entry.key] = existing
                bound = True
            else:
                bound = False
            outcomes[entry.key] = BindOutcome(
                bound=bound,
                existing_schema=existing[0],
                existing_content_hash=existing[1],
            )
        return outcomes


@pytest.fixture
def controlled_backend() -> ControlledBackend:
    return ControlledBackend()
