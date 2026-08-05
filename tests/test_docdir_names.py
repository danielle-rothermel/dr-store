"""Safe-name validation and collision-free concurrent allocation.

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

from dr_store import AllocationError, DocumentDirectory

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST_NAME = "record.json"
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
def test_unsafe_manifest_name_is_rejected_on_read(
    tmp_path: Path,
    name: str,
) -> None:
    with pytest.raises(AllocationError):
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

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=ALLOCATORS
    ) as pool:
        futures = [pool.submit(worker) for _ in range(ALLOCATORS)]
        paths = [future.result() for future in futures]

    assert len(set(paths)) == ALLOCATORS
    assert all(path.is_dir() for path in paths)
    assert len(list(tmp_path.iterdir())) == ALLOCATORS


def _allocate_in_subprocess(root: str) -> str:
    # Top-level so it is picklable for ProcessPoolExecutor spawn.
    from dr_store import DocumentDirectory

    directory = DocumentDirectory.allocate(
        root,
        prefix="run",
        manifest_name="record.json",
    )
    directory.publish({"who": directory.path.name})
    return directory.path.name


def test_cross_process_allocation_under_one_root_never_collides(
    tmp_path: Path,
) -> None:
    workers = 8
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        names = list(pool.map(_allocate_in_subprocess, [str(tmp_path)] * 8))

    assert len(set(names)) == workers
    for name in names:
        assert DocumentDirectory.read_manifest(
            tmp_path / name,
            manifest_name=MANIFEST_NAME,
        ) == {"who": name}
