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
