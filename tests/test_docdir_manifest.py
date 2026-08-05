"""Allocation, atomic durable publish, and verified Manifest read.

Every publish is one durable atomic replace: a reader sees either no
Manifest or one complete previously-published Manifest, which a reader
thread racing the writer pins here. These tests also cover the allocated
name shape, the surfaced collision, the last-write-wins publish contract,
the strict canonical read-back, and the typed failures on every read fault.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from typing import TYPE_CHECKING

import pytest

from dr_store import (
    AllocationError,
    DocumentDirectory,
    ManifestPublishError,
    ManifestReadError,
    docdir,
)

if TYPE_CHECKING:
    from pathlib import Path

    from dr_serialize import Jsonable

MANIFEST_NAME = "record.json"
PREFIX = "run"
FIRST: Jsonable = {"state": "started", "sidecars": []}
SECOND: Jsonable = {"state": "finished", "sidecars": ["stdout.bin"]}
FROZEN_NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
WATCHDOG_SECONDS = 60
PUBLICATIONS = 256
MINIMUM_OBSERVATIONS = 16


class _FrozenDatetime(dt.datetime):
    """A datetime whose ``now`` never advances, pinning allocated names."""

    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW


def _allocate(root: Path) -> DocumentDirectory:
    return DocumentDirectory.allocate(
        root,
        prefix=PREFIX,
        manifest_name=MANIFEST_NAME,
    )


def test_allocate_creates_a_fresh_prefixed_directory(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    assert directory.path.is_dir()
    assert directory.path.parent == tmp_path
    assert directory.path.name.startswith(f"{PREFIX}-")
    # Nothing is published by allocation alone.
    assert not (directory.path / MANIFEST_NAME).exists()


def test_allocate_requires_an_existing_root(tmp_path: Path) -> None:
    with pytest.raises(AllocationError) as caught:
        _allocate(tmp_path / "absent")
    assert isinstance(caught.value.__cause__, OSError)


def test_distinct_allocations_never_collide(tmp_path: Path) -> None:
    paths = {_allocate(tmp_path).path for _ in range(64)}
    assert len(paths) == 64


def test_a_name_collision_is_surfaced_and_never_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pinning uuid4 forces the second allocation onto the first's name:
    # mkdir(exist_ok=False) makes that a typed error, never a silent reuse
    # and never a retry loop.
    fixed = uuid.UUID(int=0)
    monkeypatch.setattr(docdir.uuid, "uuid4", lambda: fixed)
    monkeypatch.setattr(
        docdir.dt,
        "datetime",
        _FrozenDatetime,
    )
    first = _allocate(tmp_path)
    first.publish(FIRST)
    with pytest.raises(AllocationError) as caught:
        _allocate(tmp_path)
    assert isinstance(caught.value.__cause__, FileExistsError)
    # The occupied directory is untouched by the refused allocation.
    read_back = DocumentDirectory.read_manifest(
        first.path,
        manifest_name=MANIFEST_NAME,
    )
    assert read_back == FIRST
    assert [p.name for p in tmp_path.iterdir()] == [first.path.name]


def test_allocate_failure_is_typed_and_preserves_cause(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"")
    with pytest.raises(AllocationError) as caught:
        _allocate(blocked)
    assert isinstance(caught.value.__cause__, OSError)


def test_publish_writes_canonical_bytes(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    directory.publish({"b": 2, "a": 1})
    assert (directory.path / MANIFEST_NAME).read_bytes() == b'{"a":1,"b":2}'


def test_publish_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    assert [p.name for p in directory.path.iterdir()] == [MANIFEST_NAME]


def test_failed_publish_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    # A directory occupying the manifest path makes the atomic rename fail
    # after the temp file is written: the temp file must not survive it.
    directory = _allocate(tmp_path)
    blocked = directory.path / MANIFEST_NAME
    blocked.mkdir()
    (blocked / "occupant").write_bytes(b"x")
    with pytest.raises(ManifestPublishError) as caught:
        directory.publish(FIRST)
    assert isinstance(caught.value.__cause__, OSError)
    assert [p.name for p in directory.path.iterdir()] == [MANIFEST_NAME]


def test_publish_is_last_write_wins(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    directory.publish(SECOND)
    read_back = DocumentDirectory.read_manifest(
        directory.path,
        manifest_name=MANIFEST_NAME,
    )
    assert read_back == SECOND


def test_a_racing_reader_never_observes_a_partial_manifest(
    tmp_path: Path,
) -> None:
    # A reader thread loops read_manifest while this thread republishes
    # alternating payloads. Both start from one barrier and the reader
    # stops on an explicit event, never on elapsed time. Every observation
    # the reader collects must be one complete published document: the
    # atomic replace admits no prefix of one. A torn read raises inside the
    # reader thread, where an unasserted exception would be invisible, so
    # the failure is carried back and asserted on here.
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    start = threading.Barrier(2)
    publishing_done = threading.Event()
    observed: list[Jsonable] = []
    failures: list[BaseException] = []

    def reader() -> None:
        start.wait()
        try:
            while not publishing_done.is_set():
                observed.append(
                    DocumentDirectory.read_manifest(
                        directory.path,
                        manifest_name=MANIFEST_NAME,
                    )
                )
        except BaseException as exc:  # noqa: BLE001 - carried to the asserts
            failures.append(exc)

    watching = threading.Thread(target=reader)
    watching.start()
    try:
        start.wait()
        for index in range(PUBLICATIONS):
            directory.publish(SECOND if index % 2 else FIRST)
    finally:
        publishing_done.set()
        watching.join(timeout=WATCHDOG_SECONDS)
    assert not watching.is_alive()
    assert failures == []
    # A reader that barely ran would satisfy the payload assertion
    # vacuously, so it must have raced a meaningful share of the publishes.
    assert len(observed) >= MINIMUM_OBSERVATIONS
    assert all(seen in (FIRST, SECOND) for seen in observed)


def test_publish_rejects_non_strict_json(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    with pytest.raises(ManifestPublishError) as caught:
        directory.publish({"bad": float("nan")})
    assert caught.value.__cause__ is not None
    assert not (directory.path / MANIFEST_NAME).exists()


def test_failed_publish_preserves_the_previous_manifest(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    with pytest.raises(ManifestPublishError):
        directory.publish({"bad": float("inf")})
    read_back = DocumentDirectory.read_manifest(
        directory.path,
        manifest_name=MANIFEST_NAME,
    )
    assert read_back == FIRST


def test_read_manifest_missing_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    with pytest.raises(ManifestReadError) as caught:
        DocumentDirectory.read_manifest(
            directory.path,
            manifest_name=MANIFEST_NAME,
        )
    assert isinstance(caught.value.__cause__, OSError)


def test_read_manifest_malformed_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(b'{"truncated":')
    with pytest.raises(ManifestReadError) as caught:
        DocumentDirectory.read_manifest(
            directory.path,
            manifest_name=MANIFEST_NAME,
        )
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


def test_read_manifest_non_strict_is_typed(tmp_path: Path) -> None:
    # json.loads accepts NaN; strict validation must reject it.
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(b'{"value":NaN}')
    with pytest.raises(ManifestReadError) as caught:
        DocumentDirectory.read_manifest(
            directory.path,
            manifest_name=MANIFEST_NAME,
        )
    assert caught.value.__cause__ is not None


def test_read_manifest_non_canonical_is_typed(tmp_path: Path) -> None:
    # Decodes to the same value, but the stored bytes are not the canonical
    # rendering: byte-level drift is a read fault, not an accepted read.
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(b'{"a": 1}')
    with pytest.raises(ManifestReadError):
        DocumentDirectory.read_manifest(
            directory.path,
            manifest_name=MANIFEST_NAME,
        )


def test_read_manifest_undecodable_bytes_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(b"\xff\xfe\x00not utf-8")
    with pytest.raises(ManifestReadError) as caught:
        DocumentDirectory.read_manifest(
            directory.path,
            manifest_name=MANIFEST_NAME,
        )
    assert caught.value.__cause__ is not None
