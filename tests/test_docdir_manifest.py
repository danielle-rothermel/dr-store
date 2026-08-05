"""Allocation, atomic durable publish, and verified Manifest read.

Every publish is one durable atomic replace: a reader sees either no
Manifest or one complete previously-published Manifest. These tests pin the
allocated name shape, the last-write-wins publish contract, the strict
canonical read-back, and the typed failures on every read fault.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from dr_store import (
    AllocationError,
    DocumentDirectory,
    ManifestPublishError,
    ManifestReadError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from dr_serialize import Jsonable

MANIFEST_NAME = "record.json"
PREFIX = "run"
FIRST: Jsonable = {"state": "started", "sidecars": []}
SECOND: Jsonable = {"state": "finished", "sidecars": ["stdout.bin"]}


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


def test_republish_never_leaves_a_partial_manifest(tmp_path: Path) -> None:
    # Between two publishes, every observation of the manifest is one
    # complete previously-published document -- never a prefix of one.
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    for _ in range(32):
        directory.publish(SECOND)
        observed = DocumentDirectory.read_manifest(
            directory.path,
            manifest_name=MANIFEST_NAME,
        )
        assert observed in (FIRST, SECOND)
        directory.publish(FIRST)


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


def test_read_manifest_deeply_nested_is_typed(tmp_path: Path) -> None:
    # json.loads recurses; a hostile depth is a read fault, not a crash.
    directory = _allocate(tmp_path)
    depth = 100_000
    (directory.path / MANIFEST_NAME).write_bytes(
        b"[" * depth + b"]" * depth
    )
    with pytest.raises(ManifestReadError) as caught:
        DocumentDirectory.read_manifest(
            directory.path,
            manifest_name=MANIFEST_NAME,
        )
    assert isinstance(caught.value.__cause__, RecursionError)


def test_read_manifest_undecodable_bytes_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(b"\xff\xfe\x00not utf-8")
    with pytest.raises(ManifestReadError) as caught:
        DocumentDirectory.read_manifest(
            directory.path,
            manifest_name=MANIFEST_NAME,
        )
    assert caught.value.__cause__ is not None
