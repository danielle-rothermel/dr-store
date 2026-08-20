from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any, TypeVar

from dr_store import (
    BindStatus,
    EvictStatus,
    ObjectStore,
    PutStatus,
    SqliteBackend,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator

    from dr_serialize import Jsonable

    from dr_store.content_addressing import ObjectReference

__all__ = [
    "BlockingObjectStore",
    "SyncSessionClosedError",
    "close_all_persistent",
    "close_persistent",
    "open_sqlite",
    "persistent_sqlite",
]

_T = TypeVar("_T")
_RESULT_TIMEOUT_S = 5
_SESSION_CLOSED_MESSAGE = (
    "persistent SQLite session was closed; open a new handle"
)
_sessions_lock = threading.Lock()


class SyncSessionClosedError(RuntimeError):
    """Raised when a persistent blocking handle is used after close."""


def _blocking_result(
    future: Any,
    *,
    session: _StoreSession | None = None,
) -> _T:
    try:
        return future.result(timeout=_RESULT_TIMEOUT_S)
    except TimeoutError:
        if session is not None and session.closed:
            raise SyncSessionClosedError(_SESSION_CLOSED_MESSAGE) from None
        raise


class BlockingObjectStore:
    """Sync facade over async ``ObjectStore`` (any backend + caller's loop)."""

    def __init__(
        self,
        store: ObjectStore,
        loop: asyncio.AbstractEventLoop,
        *,
        session: _StoreSession | None = None,
    ) -> None:
        self._store = store
        self._loop = loop
        self._session = session

    def _ensure_open(self) -> None:
        if self._session is not None and self._session.closed:
            raise SyncSessionClosedError(_SESSION_CLOSED_MESSAGE)

    def _run(self, factory: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        self._ensure_open()
        future = asyncio.run_coroutine_threadsafe(factory(), self._loop)
        try:
            return _blocking_result(future, session=self._session)
        except RuntimeError as exc:
            if (
                self._session is not None
                and self._session.closed
                and str(exc) == "SQLite backend is closed"
            ):
                raise SyncSessionClosedError(_SESSION_CLOSED_MESSAGE) from None
            raise

    def put(
        self, schema: str, record: Jsonable
    ) -> tuple[ObjectReference, PutStatus]:
        return self._run(lambda: self._store.put(schema, record))

    def get(self, reference: ObjectReference) -> Jsonable:
        return self._run(lambda: self._store.get(reference))

    def bind(self, key: str, reference: ObjectReference) -> BindStatus:
        return self._run(lambda: self._store.bind(key, reference))

    def resolve(self, key: str) -> ObjectReference | None:
        return self._run(lambda: self._store.resolve(key))

    def evict_bindings(self, keys: list[str]) -> dict[str, EvictStatus]:
        return self._run(lambda: self._store.evict_bindings(keys))


class _StoreSession:
    def __init__(self, path: str) -> None:
        self._path = path
        self._open_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="dr-store-sqlite-loop",
            daemon=True,
        )
        self._backend: SqliteBackend | None = None
        self.store: BlockingObjectStore | None = None
        self._closed = False
        self._open_failure: BaseException | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def _raise_if_closed_during_open(self) -> None:
        if not self._closed:
            return
        if self.store is None and self._open_failure is not None:
            raise self._open_failure
        raise SyncSessionClosedError(_SESSION_CLOSED_MESSAGE)

    def open(self) -> BlockingObjectStore:
        if self._closed:
            self._raise_if_closed_during_open()
        with self._open_lock:
            if self.store is not None:
                return self.store
            if self._closed:
                self._raise_if_closed_during_open()
            if self._open_failure is not None:
                raise self._open_failure
            if self._thread.ident is None:
                self._thread.start()
            try:
                backend = _blocking_result(
                    asyncio.run_coroutine_threadsafe(
                        SqliteBackend.open(self._path),
                        self._loop,
                    ),
                    session=self,
                )
            except BaseException as exc:
                self._open_failure = exc
                raise
            if self._closed:
                _blocking_result(
                    asyncio.run_coroutine_threadsafe(
                        backend.aclose(),
                        self._loop,
                    ),
                    session=self,
                )
                self._raise_if_closed_during_open()
            self._backend = backend
            self.store = BlockingObjectStore(
                ObjectStore(backend),
                self._loop,
                session=self,
            )
            return self.store

    def close(self) -> None:
        with self._open_lock:
            if self._closed:
                return
            self._closed = True
            if self._backend is not None:
                with suppress(TimeoutError):
                    _blocking_result(
                        asyncio.run_coroutine_threadsafe(
                            self._backend.aclose(),
                            self._loop,
                        ),
                    )
                self._backend = None
            if self._thread.is_alive():
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)
            self.store = None


@contextmanager
def open_sqlite(path: str) -> Iterator[BlockingObjectStore]:
    """Open SQLite backend; close backend + stop event loop on exit."""
    session = _StoreSession(path)
    try:
        yield session.open()
    finally:
        session.close()


_sessions: dict[str, _StoreSession] = {}


def persistent_sqlite(path: str) -> BlockingObjectStore:
    """Open or reuse a process-lifetime session keyed by path."""
    with _sessions_lock:
        session = _sessions.get(path)
        if session is None:
            session = _StoreSession(path)
            _sessions[path] = session
    try:
        return session.open()
    except BaseException as exc:
        if session.store is None:
            session._open_failure = exc
        with _sessions_lock:
            if _sessions.get(path) is session and session.store is None:
                _sessions.pop(path, None)
        session.close()
        raise


def close_persistent(path: str) -> None:
    with _sessions_lock:
        session = _sessions.pop(path, None)
    if session is not None:
        session.close()


def close_all_persistent() -> None:
    with _sessions_lock:
        sessions = [_sessions.pop(path) for path in list(_sessions)]
    for session in sessions:
        session.close()
