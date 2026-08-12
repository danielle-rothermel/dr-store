from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_DEFAULT_CHUNK_BYTES = 1 << 16


def open_directory_descriptor(directory: Path, *, flags: int) -> int:
    return os.open(directory, flags)


def open_child_descriptor(
    name: str,
    *,
    flags: int,
    directory_descriptor: int,
) -> int:
    return os.open(name, flags, dir_fd=directory_descriptor)


def open_pinned_child(
    directory: Path,
    name: str,
    *,
    directory_flags: int,
    child_flags: int,
) -> tuple[int, int, os.stat_result]:
    """Return ``(directory_fd, child_fd, child_metadata)``.

    Caller owns cleanup.
    """
    directory_descriptor = open_directory_descriptor(
        directory,
        flags=directory_flags,
    )
    try:
        child_descriptor = open_child_descriptor(
            name,
            flags=child_flags,
            directory_descriptor=directory_descriptor,
        )
    except BaseException:
        os.close(directory_descriptor)
        raise
    metadata = os.fstat(child_descriptor)
    return directory_descriptor, child_descriptor, metadata


def read_bounded_child_descriptor(
    child_descriptor: int,
    *,
    max_bytes: int,
    chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
) -> bytes:
    """Read up to ``max_bytes + 1`` bytes from an open child descriptor."""
    chunks = bytearray()
    limit = max_bytes + 1
    while len(chunks) < limit:
        requested = min(chunk_bytes, limit - len(chunks))
        chunk = os.read(child_descriptor, requested)
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)
