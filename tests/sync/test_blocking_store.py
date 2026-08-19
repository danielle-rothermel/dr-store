from __future__ import annotations

import concurrent.futures
import tempfile
from typing import TYPE_CHECKING

import pytest

from dr_store import PutStatus
from dr_store.sync import (
    close_all_persistent,
    close_persistent,
    open_sqlite,
    persistent_sqlite,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dr_store.content_addressing import ObjectReference


@pytest.fixture
def sqlite_path() -> Iterator[str]:
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
        path = handle.name
    yield path
    close_persistent(path)


def test_put_get_round_trip(sqlite_path: str) -> None:
    with open_sqlite(sqlite_path) as store:
        reference, status = store.put("demo.record", {"value": 1})
        assert status is PutStatus.STORED
        assert store.get(reference) == {"value": 1}


def test_bind_resolve_evict(sqlite_path: str) -> None:
    from dr_store import BindStatus, EvictStatus

    with open_sqlite(sqlite_path) as store:
        reference, _ = store.put("demo.record", {"value": 1})
        assert store.bind("key", reference) is BindStatus.BOUND
        assert store.resolve("key") == reference
        evicted = store.evict_bindings(["key", "missing"])
        assert evicted == {
            "key": EvictStatus.EVICTED,
            "missing": EvictStatus.ABSENT,
        }


def test_concurrent_callers(sqlite_path: str) -> None:
    with open_sqlite(sqlite_path) as store:

        def write(index: int) -> tuple[ObjectReference, PutStatus]:
            return store.put("demo.record", {"index": index})

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(write, range(8)))
        assert all(status is PutStatus.STORED for _, status in results)
        assert len({reference.content_hash for reference, _ in results}) == 8


def test_session_lifecycle(sqlite_path: str) -> None:
    with open_sqlite(sqlite_path) as store:
        reference, _ = store.put("demo.record", {"value": 1})
    with open_sqlite(sqlite_path) as store:
        assert store.get(reference) == {"value": 1}


def test_persistent_lifecycle(sqlite_path: str) -> None:
    store = persistent_sqlite(sqlite_path)
    reference, _ = store.put("demo.record", {"value": 1})
    assert persistent_sqlite(sqlite_path) is store
    close_persistent(sqlite_path)
    close_persistent(sqlite_path)
    store = persistent_sqlite(sqlite_path)
    assert store.get(reference) == {"value": 1}
    close_all_persistent()


def test_error_propagation(sqlite_path: str) -> None:
    from dr_store.content_addressing import ObjectReference
    from dr_store.core.errors import ObjectNotFoundError

    with open_sqlite(sqlite_path) as store:
        missing = ObjectReference(
            schema="demo.record",
            content_hash="0" * 64,
        )
        with pytest.raises(ObjectNotFoundError):
            store.get(missing)
