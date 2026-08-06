from __future__ import annotations

import enum
import threading
from contextlib import contextmanager
from pathlib import Path  # noqa: TC003 - public hints resolve at runtime.
from types import (
    TracebackType,  # noqa: TC003 - public hints resolve at runtime.
)
from typing import TYPE_CHECKING, Self

from dr_serialize import (
    Jsonable,  # noqa: TC002 - public hints resolve at runtime.
)

from dr_store.content_addressing import (  # noqa: TC001
    ObjectReference,
)
from dr_store.core.errors import (
    SqliteRecordCacheClosedError,
    SqliteRecordCacheCloseError,
)
from dr_store.object_store import ObjectStore
from dr_store.record_cache.cache import CacheHit, RecordCache
from dr_store.storage_backends.sqlite import SqliteBackend

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Lifecycle(enum.Enum):
    OPEN = enum.auto()
    CLOSING = enum.auto()
    CLOSED = enum.auto()
    FAILED = enum.auto()


class _ClosePhase(enum.Enum):
    PRE_CLEANUP = enum.auto()
    CLEANUP_STARTED = enum.auto()


class _OperationLocal(threading.local):
    def __init__(self) -> None:
        self.depth = 0


class SqliteRecordCache(RecordCache):
    """Persistent cache requiring serialized first-time initialization."""

    def __init__(self, path: str | Path) -> None:
        backend = SqliteBackend._managed(path)
        super().__init__(ObjectStore(backend))
        self._sqlite_backend = backend
        self._lifecycle = threading.Condition()
        self._state = _Lifecycle.OPEN
        self._active_operations = 0
        self._operation_local = _OperationLocal()
        self._close_attempt: object | None = None
        self._close_failure: BaseException | None = None

    @contextmanager
    def _admit_operation(self) -> Iterator[None]:
        with self._lifecycle:
            if self._state is not _Lifecycle.OPEN:
                raise SqliteRecordCacheClosedError(
                    "SQLite record cache is closed"
                )
            self._active_operations += 1
            self._operation_local.depth += 1

        try:
            yield
        finally:
            with self._lifecycle:
                self._operation_local.depth -= 1
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._lifecycle.notify_all()

    def get(self, key: str, *, schema: str) -> CacheHit | None:
        with self._admit_operation():
            return super().get(key, schema=schema)

    def put(
        self,
        key: str,
        schema: str,
        record: Jsonable,
    ) -> ObjectReference:
        with self._admit_operation():
            return super().put(key, schema, record)

    def close(self) -> None:
        if self._operation_local.depth:
            raise SqliteRecordCacheCloseError(
                "cannot close SQLite record cache from an active operation"
            )
        attempt = object()
        phase = _ClosePhase.PRE_CLEANUP
        close_required = False
        owns_attempt = False
        failure: BaseException | None = None
        try:
            close_required = self._elect_closer(attempt)
            if close_required:
                owns_attempt = True
                phase = _ClosePhase.CLEANUP_STARTED
                self._sqlite_backend._close_connections()
        # Closing must publish a terminal state even for process-level exits.
        except BaseException as error:  # noqa: BLE001
            failure = error
            owns_attempt = self._owns_close_attempt(attempt)
        finally:
            try:
                self._finish_close_attempt(attempt, phase, failure)
            except BaseException as publication_error:  # noqa: BLE001
                if failure is None or (
                    isinstance(failure, Exception)
                    and not isinstance(publication_error, Exception)
                ):
                    failure = publication_error
                self._recover_close_publication(attempt, phase, failure)

        if not close_required and failure is None:
            return
        if failure is not None:
            if not owns_attempt:
                raise failure
            if not isinstance(failure, Exception):
                raise failure
            raise SqliteRecordCacheCloseError(
                "failed to close SQLite record cache"
            ) from failure

    def _elect_closer(self, attempt: object) -> bool:
        with self._lifecycle:
            while True:
                if self._state is _Lifecycle.CLOSED:
                    return False
                if self._state is _Lifecycle.FAILED:
                    assert self._close_failure is not None
                    raise SqliteRecordCacheCloseError(
                        "SQLite record cache close previously failed"
                    ) from self._close_failure
                if self._state is _Lifecycle.OPEN:
                    try:
                        self._close_attempt = attempt
                        self._state = _Lifecycle.CLOSING
                        self._lifecycle.notify_all()
                        while self._active_operations:
                            self._lifecycle.wait()
                    except BaseException:
                        if self._close_attempt is attempt:
                            self._close_attempt = None
                            self._state = _Lifecycle.OPEN
                            self._lifecycle.notify_all()
                        raise
                    else:
                        return True
                if self._state is _Lifecycle.CLOSING:
                    self._lifecycle.wait()

    def _owns_close_attempt(self, attempt: object) -> bool:
        with self._lifecycle:
            return self._close_attempt is attempt

    def _finish_close_attempt(
        self,
        attempt: object,
        phase: _ClosePhase,
        failure: BaseException | None,
    ) -> None:
        with self._lifecycle:
            if self._close_attempt is not attempt:
                return
            if failure is None:
                self._state = _Lifecycle.CLOSED
            elif phase is _ClosePhase.PRE_CLEANUP:
                self._state = _Lifecycle.OPEN
            else:
                self._close_failure = failure
                self._state = _Lifecycle.FAILED
            self._close_attempt = None
            self._lifecycle.notify_all()

    def _recover_close_publication(
        self,
        attempt: object,
        phase: _ClosePhase,
        failure: BaseException,
    ) -> None:
        with self._lifecycle:
            if self._close_attempt is attempt:
                if phase is _ClosePhase.PRE_CLEANUP:
                    self._state = _Lifecycle.OPEN
                else:
                    self._close_failure = failure
                    self._state = _Lifecycle.FAILED
                self._close_attempt = None
            self._lifecycle.notify_all()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.close()
        return False
