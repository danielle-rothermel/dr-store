"""Blocking sync facade over async ``ObjectStore``.

``open_sqlite`` and ``persistent_sqlite`` own a dedicated event-loop thread.
Close waits for every in-flight operation to finish before stopping the loop;
operation failures are delivered only to their caller, not re-raised during
close. Callers needing prompt teardown should quiesce first. Post-close calls,
and calls racing close after registration, raise ``SyncSessionClosedError``
rather than hanging. Cancel-on-close remains a future upgrade path if fast
teardown is ever needed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from dr_store import (
    BindStatus,
    EvictStatus,
    ObjectStore,
    PutStatus,
    SqliteBackend,
    SqliteBackendClosedError,
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
_LOGGER = logging.getLogger(__name__)
_SESSION_CLOSED_MESSAGE = (
    "persistent SQLite session was closed; open a new handle"
)
_THREAD_JOIN_WATCHDOG_S = 5
_sessions_lock = threading.Lock()


class SyncSessionClosedError(RuntimeError):
    """Raised when a persistent blocking handle is used after close."""


@dataclass(frozen=True, slots=True)
class _OpenFailure:
    exc_type: type[BaseException]
    args: tuple[object, ...]
    kwargs: tuple[tuple[str, object], ...]
    message: str
    cause: BaseException | None

    @classmethod
    def capture(cls, exc: BaseException) -> _OpenFailure:
        kwargs = tuple(
            (key, value)
            for key, value in exc.__dict__.items()
            if not key.startswith("_")
        )
        return cls(
            exc_type=type(exc),
            args=exc.args,
            kwargs=kwargs,
            message=str(exc),
            cause=exc.__cause__,
        )

    def reraise(self) -> None:
        fresh: BaseException | None = None
        kwargs = dict(self.kwargs)
        for attempt in (
            lambda: self.exc_type(**kwargs) if kwargs else None,
            lambda: self.exc_type(*self.args),
            lambda: RuntimeError(self.message),
        ):
            try:
                candidate = attempt()
                if candidate is not None:
                    fresh = candidate
                    break
            except (TypeError, ValueError):
                continue
        assert fresh is not None
        raise fresh from self.cause


def _drain_inflight_futures(
    snapshot: frozenset[concurrent.futures.Future[Any]],
) -> None:
    if not snapshot:
        return
    done, _ = concurrent.futures.wait(snapshot)
    for future in done:
        with suppress(concurrent.futures.CancelledError):
            future.exception()


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
        session = self._session
        if session is not None:
            with session._futures_lock:
                if session.closed:
                    raise SyncSessionClosedError(_SESSION_CLOSED_MESSAGE)
                future = asyncio.run_coroutine_threadsafe(
                    factory(),
                    self._loop,
                )
                session._futures.add(future)
        else:
            future = asyncio.run_coroutine_threadsafe(factory(), self._loop)
        try:
            return future.result()
        except concurrent.futures.CancelledError as exc:
            if session is not None and session.closed:
                raise SyncSessionClosedError(_SESSION_CLOSED_MESSAGE) from exc
            raise
        except SqliteBackendClosedError as exc:
            if session is not None and session.closed:
                raise SyncSessionClosedError(_SESSION_CLOSED_MESSAGE) from exc
            raise
        finally:
            if session is not None:
                with session._futures_lock:
                    session._futures.discard(future)

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
        self._futures_lock = threading.Lock()
        self._futures: set[concurrent.futures.Future[Any]] = set()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="dr-store-sqlite-loop",
            daemon=True,
        )
        self._backend: SqliteBackend | None = None
        self.store: BlockingObjectStore | None = None
        self._closed = False
        self._open_failure: _OpenFailure | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def _raise_if_closed_during_open(self) -> None:
        if not self._closed:
            return
        if self.store is None and self._open_failure is not None:
            self._open_failure.reraise()
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
                self._open_failure.reraise()
            if self._thread.ident is None:
                self._thread.start()
            try:
                backend = asyncio.run_coroutine_threadsafe(
                    SqliteBackend.open(self._path),
                    self._loop,
                ).result()
            except Exception as exc:
                self._open_failure = _OpenFailure.capture(exc)
                raise
            self._backend = backend
            self.store = BlockingObjectStore(
                ObjectStore(backend),
                self._loop,
                session=self,
            )
            return self.store

    def close(self) -> None:
        with self._open_lock:
            if (
                self._closed
                and not self._thread.is_alive()
                and self._backend is None
            ):
                return
            with self._futures_lock:
                self._closed = True
                snapshot = frozenset(self._futures)
            _drain_inflight_futures(snapshot)
            close_error: BaseException | None = None
            try:
                if self._backend is not None:
                    asyncio.run_coroutine_threadsafe(
                        self._backend.aclose(),
                        self._loop,
                    ).result()
            except Exception as exc:  # noqa: BLE001 - preserve close failure for reraise
                close_error = exc
            finally:
                if self._thread.is_alive():
                    self._loop.call_soon_threadsafe(self._loop.stop)
                    self._thread.join(timeout=_THREAD_JOIN_WATCHDOG_S)
                    if self._thread.is_alive():
                        _LOGGER.warning(
                            "SQLite event-loop thread did not stop within %ss",
                            _THREAD_JOIN_WATCHDOG_S,
                        )
                if not self._thread.is_alive() and not self._loop.is_closed():
                    self._loop.close()
                self._backend = None
                self.store = None
            if close_error is not None:
                raise close_error


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
    opened: BlockingObjectStore | None = None
    try:
        opened = session.open()
        return opened
    finally:
        if opened is None:
            with _sessions_lock:
                if _sessions.get(path) is session:
                    _sessions.pop(path, None)
            session.close()


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
