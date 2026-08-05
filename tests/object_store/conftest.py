from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dr_store import BindOutcome, PutOutcome


@dataclass(slots=True)
class ControlledBackend:
    object_row: tuple[str, str, str] | None = None
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

    def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        self.put_calls += 1
        if self.object_row is None:
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
        stored_schema, _stored_hash, stored_canonical = self.object_row
        return PutOutcome(
            inserted=False,
            stored_schema=stored_schema,
            stored_canonical=stored_canonical,
        )

    def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        del schema
        if self.object_row is None:
            return None
        stored_schema, stored_hash, stored_canonical = self.object_row
        if stored_hash != content_hash:
            return None
        return (stored_schema, stored_canonical)

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


@pytest.fixture
def controlled_backend() -> ControlledBackend:
    return ControlledBackend()
