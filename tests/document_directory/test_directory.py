from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from dr_store import AllocationError, DocumentDirectory
from dr_store.document_directory import directory as directory_module

if TYPE_CHECKING:
    from dr_serialize import Jsonable

MANIFEST_NAME = "record.json"
MANIFEST_MAX_BYTES = 1 << 20
PREFIX = "run"
PUBLISHED: Jsonable = {"state": "started", "sidecars": []}
FROZEN_NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
UNSAFE_NAMES = (
    "",
    ".",
    "..",
    "/",
    "nested/name",
    "../escape",
    "trailing/",
    "back\\slash",
    "nul\x00byte",
)


class _FrozenDatetime(dt.datetime):
    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW


def _allocate(root: Path) -> DocumentDirectory:
    return DocumentDirectory.allocate(
        root,
        prefix=PREFIX,
        manifest_name=MANIFEST_NAME,
        manifest_max_bytes=MANIFEST_MAX_BYTES,
    )


def test_allocate_creates_a_fresh_prefixed_directory(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    assert directory.path.is_dir()
    assert directory.path.parent == tmp_path
    assert directory.path.name.startswith(f"{PREFIX}-")
    assert not (directory.path / MANIFEST_NAME).exists()


def test_relative_directory_path_is_captured_for_all_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "document").mkdir(parents=True)
    (second / "document").mkdir(parents=True)
    monkeypatch.chdir(first)
    directory = DocumentDirectory(
        Path("document"),
        MANIFEST_NAME,
        manifest_max_bytes=MANIFEST_MAX_BYTES,
    )

    monkeypatch.chdir(second)
    directory.publish(PUBLISHED)
    writer = directory.open_sidecar("stdout.bin")
    writer.write(b"output")
    writer.finalize()

    assert directory.path == first / "document"
    assert directory.read_manifest() == PUBLISHED
    assert {path.name for path in directory.path.iterdir()} == {
        MANIFEST_NAME,
        "stdout.bin",
    }
    assert list((second / "document").iterdir()) == []


def test_allocate_failure_is_typed_and_preserves_cause(
    tmp_path: Path,
) -> None:
    with pytest.raises(AllocationError) as caught:
        _allocate(tmp_path / "absent")
    assert isinstance(caught.value.__cause__, OSError)


def test_a_name_collision_is_surfaced_and_never_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = uuid.UUID(int=0)
    monkeypatch.setattr(directory_module.uuid, "uuid4", lambda: fixed)
    monkeypatch.setattr(directory_module.dt, "datetime", _FrozenDatetime)
    first = _allocate(tmp_path)
    first.publish(PUBLISHED)

    with pytest.raises(AllocationError) as caught:
        _allocate(tmp_path)

    assert isinstance(caught.value.__cause__, FileExistsError)
    assert first.read_manifest() == PUBLISHED
    assert [path.name for path in tmp_path.iterdir()] == [first.path.name]


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_prefix_is_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(AllocationError):
        DocumentDirectory.allocate(
            tmp_path,
            prefix=name,
            manifest_name=MANIFEST_NAME,
            manifest_max_bytes=MANIFEST_MAX_BYTES,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_manifest_name_is_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(AllocationError):
        DocumentDirectory.allocate(
            tmp_path,
            prefix=PREFIX,
            manifest_name=name,
            manifest_max_bytes=MANIFEST_MAX_BYTES,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_manifest_name_is_rejected_on_construction(
    tmp_path: Path,
    name: str,
) -> None:
    with pytest.raises(AllocationError):
        DocumentDirectory(
            tmp_path,
            name,
            manifest_max_bytes=MANIFEST_MAX_BYTES,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_invalid_manifest_bounds_are_rejected_before_allocation(
    tmp_path: Path,
    limit: object,
) -> None:
    with pytest.raises(AllocationError):
        DocumentDirectory.allocate(
            tmp_path,
            prefix=PREFIX,
            manifest_name=MANIFEST_NAME,
            manifest_max_bytes=cast("int", limit),
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_sidecar_name_is_rejected(tmp_path: Path, name: str) -> None:
    directory = _allocate(tmp_path)
    with pytest.raises(AllocationError):
        directory.open_sidecar(name)
    assert list(directory.path.iterdir()) == []


@pytest.mark.parametrize(
    "name",
    [MANIFEST_NAME, ".dr-store-document-owned", MANIFEST_NAME.upper()],
)
def test_sidecar_cannot_take_the_manifest_or_reserved_temp_namespace(
    tmp_path: Path,
    name: str,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(PUBLISHED)

    with pytest.raises(AllocationError):
        directory.open_sidecar(name)

    assert directory.read_manifest() == PUBLISHED


def test_several_sidecars_live_side_by_side(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    summaries = {}
    for name in ("stdout.bin", "stderr.bin"):
        writer = directory.open_sidecar(name)
        writer.write(name.encode())
        summaries[name] = writer.finalize()

    for name, summary in summaries.items():
        directory.verify_sidecar(
            name,
            expected_digest=summary.digest,
            expected_head_length=summary.head_length,
            expected_tail_length=summary.tail_length,
        )


def test_unopenable_sidecar_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / "stdout.bin").mkdir()
    with pytest.raises(AllocationError) as caught:
        directory.open_sidecar("stdout.bin")
    assert isinstance(caught.value.__cause__, OSError)
