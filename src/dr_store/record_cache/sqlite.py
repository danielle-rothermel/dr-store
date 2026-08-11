from __future__ import annotations

import asyncio
import enum
from collections.abc import (  # noqa: TC003 - public hints resolve at runtime.
    AsyncIterator,
    Iterable,
    Mapping,
)
from contextlib import asynccontextmanager
from pathlib import Path  # noqa: TC003 - public hints resolve at runtime.
from types import (  # noqa: TC003 - public hints resolve at runtime.
    TracebackType,
)
from typing import Self

from dr_serialize import (
    Jsonable,  # noqa: TC002 - public hints resolve at runtime.
)

from dr_store.content_addressing import ObjectReference  # noqa: TC001
from dr_store.core.errors import (
    SqliteRecordCacheClosedError,
    SqliteRecordCacheCloseError,
)
from dr_store.object_store import ObjectStore
from dr_store.record_cache.cache import CacheEntry, CacheHit, RecordCache
from dr_store.storage_backends.sqlite import SqliteBackend, _await_settled


class _Lifecycle(enum.Enum):
    OPEN = enum.auto()
    CLOSING = enum.auto()
    CLOSED = enum.auto()
    FAILED = enum.auto()


class SqliteRecordCache(RecordCache):
    """Long-lived asynchronous cache owning one SQLite backend lifecycle."""

    _sqlite_backend: SqliteBackend
    _loop: asyncio.AbstractEventLoop
    _state: _Lifecycle
    _active_operations: int
    _operation_tasks: set[asyncio.Task[object]]
    _drained: asyncio.Event
    _close_task: asyncio.Task[None] | None

    def __init__(self, path: str | Path) -> None:
        del path
        raise TypeError("use 'await SqliteRecordCache.open(path)'")

    @classmethod
    async def open(
        cls,
        path: str | Path,
        *,
        busy_timeout_ms: int = 30_000,
    ) -> Self:
        """Open a persistent record cache on ``path``.

        ``busy_timeout_ms`` is forwarded to the owned SQLite backend; when the
        bound expires, SQLite raises ``OperationalError`` rather than retrying
        indefinitely.
        """
        backend = await SqliteBackend.open(
            path,
            busy_timeout_ms=busy_timeout_ms,
        )
        self = object.__new__(cls)
        RecordCache.__init__(self, ObjectStore(backend))
        self._sqlite_backend = backend
        self._loop = asyncio.get_running_loop()
        self._state = _Lifecycle.OPEN
        self._active_operations = 0
        self._operation_tasks = set()
        self._drained = asyncio.Event()
        self._drained.set()
        self._close_task = None
        return self

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError(
                "SQLite record cache must be used on the event loop that "
                "opened it"
            )

    @asynccontextmanager
    async def _admit_operation(self) -> AsyncIterator[None]:
        self._check_loop()
        if self._state is not _Lifecycle.OPEN:
            raise SqliteRecordCacheClosedError("SQLite record cache is closed")
        task = asyncio.current_task()
        assert task is not None
        self._active_operations += 1
        self._operation_tasks.add(task)
        self._drained.clear()
        try:
            yield
        finally:
            self._operation_tasks.remove(task)
            self._active_operations -= 1
            if self._active_operations == 0:
                self._drained.set()

    async def get(self, key: str, *, schema: str) -> CacheHit | None:
        async with self._admit_operation():
            return await super().get(key, schema=schema)

    async def put(
        self,
        key: str,
        schema: str,
        record: Jsonable,
    ) -> ObjectReference:
        async with self._admit_operation():
            return await super().put(key, schema, record)

    async def get_many(
        self,
        keys: Iterable[str],
        *,
        schema: str,
    ) -> dict[str, CacheHit | None]:
        async with self._admit_operation():
            return await super().get_many(keys, schema=schema)

    async def put_many(
        self,
        entries: Mapping[str, CacheEntry],
    ) -> dict[str, ObjectReference]:
        async with self._admit_operation():
            return await super().put_many(entries)

    async def aclose(self) -> None:
        if self._state is _Lifecycle.CLOSED:
            return
        self._check_loop()
        task = asyncio.current_task()
        if task in self._operation_tasks:
            raise SqliteRecordCacheCloseError(
                "cannot close SQLite record cache from an active operation"
            )
        if self._close_task is None:
            self._state = _Lifecycle.CLOSING
            self._close_task = self._loop.create_task(self._close_resources())
        await _await_settled(self._close_task)

    async def _close_resources(self) -> None:
        try:
            await self._drained.wait()
            await self._sqlite_backend.aclose()
        except Exception as error:
            self._state = _Lifecycle.FAILED
            raise SqliteRecordCacheCloseError(
                "failed to close SQLite record cache"
            ) from error
        else:
            self._state = _Lifecycle.CLOSED

    async def __aenter__(self) -> Self:
        self._check_loop()
        if self._state is not _Lifecycle.OPEN:
            raise SqliteRecordCacheClosedError("SQLite record cache is closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, exc_value, traceback
        await self.aclose()
        return False
