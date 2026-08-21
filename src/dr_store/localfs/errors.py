from __future__ import annotations

from enum import UNIQUE, StrEnum, verify
from pathlib import Path  # noqa: TC003 - public hints resolve at runtime.


@verify(UNIQUE)
class PrivatePathReason(StrEnum):
    """Why a path was refused by private-directory policy.

    Members describe reporting outcomes. Refusal behavior must never be
    constructed by iterating this enum.
    """

    NOT_OWNED = "not_owned"
    WRONG_TYPE = "wrong_type"
    HARD_LINKED = "hard_linked"
    SYMLINKED = "symlinked"
    FILESYSTEM_ROOT = "filesystem_root"


class LocalFsError(RuntimeError):
    """Base for localfs policy and lifecycle failures."""


class PrivatePathViolationError(LocalFsError):
    """A path failed private-directory ownership, type, or link policy."""

    def __init__(self, *, path: Path, reason: PrivatePathReason) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"private path {str(path)!r} refused: {reason.value}")


class LockNotHeldError(LocalFsError):
    """``FileLock.directory`` was read before enter or after exit."""

    def __init__(self) -> None:
        super().__init__("file lock is not held")


class PrivateDirectoryClosedError(LocalFsError):
    """An operation ran against a closed ``PrivateDirectory``."""

    def __init__(self) -> None:
        super().__init__("private directory is already closed")
