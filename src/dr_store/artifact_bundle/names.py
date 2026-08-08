from __future__ import annotations

from dr_store.artifact_bundle._wire import (
    BUNDLE_TEMP_PREFIX,
    MANIFEST_NAME,
)

_UNSAFE_NAME_CHARACTERS = frozenset({"/", "\\", "\x00"})
_RESERVED_NAMES = frozenset({"", ".", ".."})


def validate_single_segment(name: str, *, role: str) -> None:
    if name in _RESERVED_NAMES:
        raise ValueError(f"{role} must be a safe name, got {name!r}")
    if any(character in _UNSAFE_NAME_CHARACTERS for character in name):
        raise ValueError(
            f"{role} must be one relative path segment, got {name!r}"
        )


def validate_artifact_name(name: str) -> None:
    validate_single_segment(name, role="artifact name")
    if name == MANIFEST_NAME or name.startswith(BUNDLE_TEMP_PREFIX):
        raise ValueError(f"artifact name {name!r} is reserved by the bundle")
