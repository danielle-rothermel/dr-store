"""In-memory backend for tests and single-process use.

Two dictionaries under one lock implement the append-only object and
binding tables. Atomicity holds within a single process only; for durable
cross-process use choose :class:`~dr_store.backends.sqlite.SqliteBackend`.
"""

from __future__ import annotations

import threading

from dr_store.backends.types import BindOutcome, PutOutcome


class MemoryBackend:
    """Process-local append-only object and binding tables."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # content_hash -> (schema, canonical)
        self._objects: dict[str, tuple[str, str]] = {}
        # key -> (schema, content_hash)
        self._bindings: dict[str, tuple[str, str]] = {}

    def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        with self._lock:
            existing = self._objects.get(content_hash)
            if existing is None:
                self._objects[content_hash] = (schema, canonical)
                return PutOutcome(
                    inserted=True,
                    stored_schema=schema,
                    stored_canonical=canonical,
                )
            stored_schema, stored_canonical = existing
            return PutOutcome(
                inserted=False,
                stored_schema=stored_schema,
                stored_canonical=stored_canonical,
            )

    def get_object(
        self,
        *,
        content_hash: str,
    ) -> tuple[str, str] | None:
        with self._lock:
            return self._objects.get(content_hash)

    def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome:
        with self._lock:
            existing = self._bindings.get(key)
            if existing is None:
                self._bindings[key] = (schema, content_hash)
                return BindOutcome(
                    bound=True,
                    existing_schema=schema,
                    existing_content_hash=content_hash,
                )
            existing_schema, existing_hash = existing
            return BindOutcome(
                bound=False,
                existing_schema=existing_schema,
                existing_content_hash=existing_hash,
            )

    def get_binding(self, *, key: str) -> tuple[str, str] | None:
        with self._lock:
            return self._bindings.get(key)
