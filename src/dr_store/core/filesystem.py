from __future__ import annotations

from dr_store.core.errors import AllocationError

_UNSAFE_NAME_CHARACTERS = frozenset({"/", "\\", "\x00"})
_RESERVED_NAMES = frozenset({"", ".", ".."})


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
    error: type[Exception] = AllocationError,
) -> None:
    """Require one path segment, preserving the caller's error taxonomy.

    Validation is lexical only; it neither inspects nor fences existing
    entries.
    """
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
    error: type[Exception] = AllocationError,
) -> None:
    """Require one safe document-directory allocation prefix."""
    validate_safe_name(prefix, role="prefix", error=error)


def validate_lexical_sidecar_name(
    name: str,
    *,
    error: type[Exception] = AllocationError,
) -> None:
    """Require one safe sidecar name segment."""
    validate_safe_name(name, role="sidecar name", error=error)
