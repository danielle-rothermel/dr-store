from __future__ import annotations

from dr_store.core.errors import AllocationError

_UNSAFE_NAME_CHARACTERS = frozenset({"/", "\\", "\x00"})
_RESERVED_NAMES = frozenset({"", ".", ".."})


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
    if name in _RESERVED_NAMES:
        raise error(f"{role} must be a safe name, got {name!r}")
    if any(char in _UNSAFE_NAME_CHARACTERS for char in name):
        raise error(
            f"{role} must be a single path segment with no separator, "
            f"got {name!r}"
        )
