from __future__ import annotations

from enum import UNIQUE, StrEnum, verify


@verify(UNIQUE)
class ContentMismatchReason(StrEnum):
    HASH_MISMATCH = "hash_mismatch"
    INVALID_JSON = "invalid_json"
    NON_CANONICAL_PROFILE = "non_canonical_profile"
    NON_CANONICAL_FORM = "non_canonical_form"


@verify(UNIQUE)
class RegularChildFailureReason(StrEnum):
    """Why a bounded child read or verification did not succeed."""

    MISSING = "missing"
    NOT_REGULAR = "not_regular"
    MISMATCH = "mismatch"
    BOUNDS_EXCEEDED = "bounds_exceeded"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
