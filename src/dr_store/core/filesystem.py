from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING

from dr_store.core.errors import AllocationError, DocumentDirectoryError

if TYPE_CHECKING:
    from types import ModuleType

try:
    import fcntl as _fcntl

    fcntl: ModuleType | None = _fcntl
except ImportError:  # pragma: no cover - exercised only off POSIX
    fcntl = None

_UNSAFE_NAME_CHARACTERS = frozenset({"/", "\\", "\x00"})
_RESERVED_NAMES = frozenset({"", ".", ".."})
_UNSUPPORTED_FULL_FSYNC_ERRNOS = frozenset(
    {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
)


def validate_safe_name(
    name: str,
    *,
    role: str,
    error: type[DocumentDirectoryError] = AllocationError,
) -> None:
    """Require one path segment, preserving the caller's error taxonomy.

    Validation is lexical only; it neither inspects nor fences existing
    entries.
    """
    if name in _RESERVED_NAMES:
        raise error(f"{role} must be a safe name, got {name!r}")
    if any(char in _UNSAFE_NAME_CHARACTERS for char in name):
        raise error(
            f"{role} must be a single path segment with no separator, "
            f"got {name!r}"
        )


def flush_descriptor(descriptor: int) -> None:
    """Flush bytes, preferring macOS ``F_FULLFSYNC``.

    Filesystems that do not support ``F_FULLFSYNC`` fall back to ``fsync``.
    """
    full_fsync = getattr(fcntl, "F_FULLFSYNC", None)
    if fcntl is not None and full_fsync is not None:
        try:
            fcntl.fcntl(descriptor, full_fsync)
        except OSError as error:
            if error.errno not in _UNSUPPORTED_FULL_FSYNC_ERRNOS:
                raise
            os.fsync(descriptor)
        return
    os.fsync(descriptor)
