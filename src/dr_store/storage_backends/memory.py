from __future__ import annotations

import threading

from dr_store.storage_backends.contract import BindOutcome, PutOutcome


class MemoryBackend:
    """Thread-safe, process-local object and binding storage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._objects: dict[tuple[str, str], str] = {}
        self._bindings: dict[str, tuple[str, str]] = {}

    def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome:
        with self._lock:
            existing = self._objects.get((schema, content_hash))
            if existing is None:
                self._objects[(schema, content_hash)] = canonical
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
        with self._lock:
            # Alternate schemas distinguish mismatch from missing content.
            exact = self._objects.get((schema, content_hash))
            if exact is not None:
                return (schema, exact)
            for (row_schema, row_hash), canonical in self._objects.items():
                if row_hash == content_hash:
                    return (row_schema, canonical)
            return None

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
