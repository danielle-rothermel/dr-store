from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from dr_store import MemoryBackend, PutOutcome

if TYPE_CHECKING:
    from dr_store.storage_backends.contract import BindOutcome


@dataclass(slots=True)
class ControlledBackend:
    """Instrument MemoryBackend without reimplementing storage semantics."""

    _inner: MemoryBackend = field(default_factory=MemoryBackend)
    put_calls: int = 0

    @property
    def object_rows(self) -> dict[tuple[str, str], str]:
        return self._inner._objects

    @property
    def bindings(self) -> dict[str, tuple[str, str]]:
        return self._inner._bindings

    def set_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> None:
        self._inner._objects[(schema, content_hash)] = canonical

    async def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        self.put_calls += 1
        return await self._inner.put_object(
            schema=schema,
            content_hash=content_hash,
            canonical=canonical,
        )

    async def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None:
        return await self._inner.get_object(
            schema=schema,
            content_hash=content_hash,
        )

    async def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        return await self._inner.bind(
            key=key,
            schema=schema,
            content_hash=content_hash,
        )

    async def get_binding(self, *, key: str) -> tuple[str, str] | None:
        return await self._inner.get_binding(key=key)

    async def get_bound_objects(self, *, keys: tuple[str, ...]):
        return await self._inner.get_bound_objects(keys=keys)

    async def put_bound_objects(self, *, entries: tuple):
        self.put_calls += 1
        return await self._inner.put_bound_objects(entries=entries)


@pytest.fixture
def controlled_backend() -> ControlledBackend:
    return ControlledBackend()
