"""Document Directory names and collision-free concurrent allocation.

Prefixes, Manifest names, and Sidecar names are single path segments: a name
carrying a separator, a NUL byte, or a reserved ``.``/``..`` can never
escape the directory it is joined to. Allocation itself is collision-free
under one root without any locking.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import TYPE_CHECKING

import pytest

from dr_store import AllocationError, DocumentDirectory, ManifestReadError

if TYPE_CHECKING:
    from pathlib import Path

    from dr_serialize import Jsonable

MANIFEST_NAME = "record.json"
PUBLISHED: Jsonable = {"state": "started", "sidecars": []}
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
ALLOCATORS = 32


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_prefix_is_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(AllocationError):
        DocumentDirectory.allocate(
            tmp_path,
            prefix=name,
            manifest_name=MANIFEST_NAME,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_manifest_name_is_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(AllocationError):
        DocumentDirectory.allocate(
            tmp_path,
            prefix="run",
            manifest_name=name,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_manifest_name_is_rejected_on_construction(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(AllocationError):
        DocumentDirectory(tmp_path, name)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_manifest_name_is_rejected_on_read(
    tmp_path: Path,
    name: str,
) -> None:
    # A read path allocates nothing, so its name fault is a read fault.
    with pytest.raises(ManifestReadError):
        DocumentDirectory.read_manifest(tmp_path, manifest_name=name)


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_sidecar_name_is_rejected(tmp_path: Path, name: str) -> None:
    directory = DocumentDirectory.allocate(
        tmp_path,
        prefix="run",
        manifest_name=MANIFEST_NAME,
    )
    with pytest.raises(AllocationError):
        directory.open_sidecar(name)
    assert list(directory.path.iterdir()) == []


@pytest.mark.parametrize(
    "name",
    [MANIFEST_NAME, MANIFEST_NAME + ".tmp", MANIFEST_NAME.upper()],
)
def test_sidecar_cannot_take_the_manifest_or_its_temp_name(
    tmp_path: Path,
    name: str,
) -> None:
    # open_sidecar truncates on open, so the manifest name would destroy a
    # published manifest and the temp name would be consumed by the next
    # publish. The case variant matters because the durability claim is
    # scoped to case-insensitive local macOS filesystems.
    directory = DocumentDirectory.allocate(
        tmp_path,
        prefix="run",
        manifest_name=MANIFEST_NAME,
    )
    directory.publish(PUBLISHED)
    with pytest.raises(AllocationError):
        directory.open_sidecar(name)
    read_back = DocumentDirectory.read_manifest(
        directory.path,
        manifest_name=MANIFEST_NAME,
    )
    assert read_back == PUBLISHED


def test_concurrent_allocation_under_one_root_never_collides(
    tmp_path: Path,
) -> None:
    start = threading.Barrier(ALLOCATORS)

    def worker() -> Path:
        start.wait()
        return DocumentDirectory.allocate(
            tmp_path,
            prefix="run",
            manifest_name=MANIFEST_NAME,
        ).path

    with concurrent.futures.ThreadPoolExecutor(max_workers=ALLOCATORS) as pool:
        futures = [pool.submit(worker) for _ in range(ALLOCATORS)]
        paths = [future.result() for future in futures]

    assert len(set(paths)) == ALLOCATORS
    assert all(path.is_dir() for path in paths)
    assert len(list(tmp_path.iterdir())) == ALLOCATORS
