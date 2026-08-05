"""Filesystem safety and durability primitives for dr-store."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from dr_store.core.errors import AllocationError, DocumentDirectoryError

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

try:  # POSIX only; the flush ladder falls back to os.fsync without it.
    import fcntl as _fcntl

    fcntl: ModuleType | None = _fcntl
except ImportError:  # pragma: no cover - exercised only off POSIX
    fcntl = None

_UNSAFE_NAME_CHARACTERS = frozenset({"/", "\\", "\x00"})
_RESERVED_NAMES = frozenset({"", ".", ".."})


def validate_safe_name(
    name: str,
    *,
    role: str,
    error: type[DocumentDirectoryError] = AllocationError,
) -> None:
    """Raise ``error`` unless ``name`` is a safe single path segment.

    A safe name is a non-empty string that is neither ``.`` nor ``..`` and
    contains no path separator or NUL byte, so it can only ever name a child
    of the directory it is joined to. The raised class follows the path the
    name arrived on, so a read fault never surfaces as an allocation failure.
    """
    if name in _RESERVED_NAMES:
        raise error(f"{role} must be a safe name, got {name!r}")
    if any(char in _UNSAFE_NAME_CHARACTERS for char in name):
        raise error(
            f"{role} must be a single path segment with no separator, "
            f"got {name!r}"
        )


def flush_descriptor(descriptor: int) -> None:
    """Force written bytes to the storage medium, not just the OS cache.

    macOS ``fsync`` only pushes to the drive's write cache, so the platform
    ladder is ``F_FULLFSYNC`` first and ``os.fsync`` as the fallback -- where
    ``fcntl`` itself is absent, where the fcntl command is absent, and where
    the filesystem rejects it.
    """
    full_fsync = getattr(fcntl, "F_FULLFSYNC", None)
    if fcntl is not None and full_fsync is not None:
        try:
            fcntl.fcntl(descriptor, full_fsync)
        except OSError:
            os.fsync(descriptor)
        return
    os.fsync(descriptor)


def flush_directory(directory: Path) -> None:
    """Flush a directory entry so a rename survives abrupt process death."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        flush_descriptor(descriptor)
    finally:
        os.close(descriptor)
