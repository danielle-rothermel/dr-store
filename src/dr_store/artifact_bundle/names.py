from __future__ import annotations

from dr_store.artifact_bundle._wire import (
    BUNDLE_TEMP_PREFIX,
    MANIFEST_NAME,
)
from dr_store.document_file import is_reserved_document_temp_name

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
    """Validate a descriptor name against the wire format's own rule.

    This is the rule the recorded format has always enforced, so it is what
    reading applies to descriptors it parses. Admission is stricter than the
    format: see `validate_admissible_artifact_name`.
    """
    validate_single_segment(name, role="artifact name")
    if name == MANIFEST_NAME or name.startswith(BUNDLE_TEMP_PREFIX):
        raise ValueError(f"artifact name {name!r} is reserved by the bundle")


def validate_admissible_artifact_name(name: str) -> None:
    """Validate a caller-chosen name a publication is asked to create.

    Manifest publication creates its temporary document in the
    document-directory namespace, so admission additionally refuses that
    namespace through the document layer's own predicate: a caller-chosen name
    occupying it would deny publication. Reading does not apply this rule,
    because a bundle an earlier release recorded may legitimately carry such a
    name and stays readable.
    """
    validate_artifact_name(name)
    if is_reserved_document_temp_name(name):
        raise ValueError(f"artifact name {name!r} is reserved by the bundle")
