from __future__ import annotations

import os
import stat

_UNSAFE_NAME_CHARACTERS = frozenset({"/", "\\", "\x00"})
_RESERVED_NAMES = frozenset({"", ".", ".."})

_READ_CHUNK_BYTES = 1 << 16
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())
_REQUIRED_PINNED_READ_OPEN_FLAGS = (
    "O_CLOEXEC",
    "O_DIRECTORY",
    "O_NOFOLLOW",
    "O_NONBLOCK",
)


class UnsafeNameError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def check_safe_name(name: str, *, role: str) -> None:
    """Require one path segment.

    Raises :class:`UnsafeNameError` on failure.
    """
    if name in _RESERVED_NAMES:
        raise UnsafeNameError(f"{role} must be a safe name, got {name!r}")
    if any(char in _UNSAFE_NAME_CHARACTERS for char in name):
        raise UnsafeNameError(
            f"{role} must be a single path segment with no separator, "
            f"got {name!r}"
        )


def validate_safe_name(
    name: str,
    *,
    role: str,
    error: type[Exception] | None = None,
) -> None:
    """Require one path segment, preserving the caller's error taxonomy.

    Validation is lexical only; it neither inspects nor fences existing
    entries.
    """
    if error is None:
        from dr_store.core.errors import AllocationError  # noqa: PLC0415

        error = AllocationError
    try:
        check_safe_name(name, role=role)
    except UnsafeNameError as exc:
        raise error(exc.message) from None


def check_regular_child_name(name: str) -> None:
    """Require one safe regular-child name segment."""
    check_safe_name(name, role="child name")


def validate_directory_prefix(
    prefix: str,
    *,
    error: type[Exception] | None = None,
) -> None:
    """Require one safe document-directory allocation prefix."""
    validate_safe_name(prefix, role="prefix", error=error)


def validate_lexical_sidecar_name(
    name: str,
    *,
    error: type[Exception] | None = None,
) -> None:
    """Require one safe sidecar name segment."""
    validate_safe_name(name, role="sidecar name", error=error)


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _child_read_open_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _pinned_read_support_detail() -> str | None:
    missing_flags = [
        flag
        for flag in _REQUIRED_PINNED_READ_OPEN_FLAGS
        if not isinstance(getattr(os, flag, None), int)
    ]
    if not _OPEN_SUPPORTS_DIR_FD or missing_flags:
        return ", ".join(missing_flags) or "os.open(dir_fd=...)"
    return None


def _require_regular_file(
    metadata: os.stat_result,
    *,
    error: type[Exception],
    message: str,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise error(message)
