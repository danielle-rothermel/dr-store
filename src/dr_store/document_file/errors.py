from __future__ import annotations

from enum import UNIQUE, StrEnum, verify
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@verify(UNIQUE)
class PublicationStage(StrEnum):
    """The phase in which a document publication failed.

    Members describe reporting phases. Publication behavior must never be
    constructed by iterating this enum.
    """

    ENCODE = "encode"
    CREATE_TEMP = "create_temp"
    WRITE_TEMP = "write_temp"
    FLUSH_TEMP = "flush_temp"
    REPLACE_TARGET = "replace_target"
    FLUSH_DIRECTORY = "flush_directory"


class DocumentFileError(Exception):
    """Base for standalone canonical document-file failures."""


class DocumentPublishError(DocumentFileError):
    """A failed publication with explicit phase and replacement state."""

    def __init__(
        self,
        path: Path,
        stage: PublicationStage,
        *,
        replacement_completed: bool,
    ) -> None:
        self.path = path
        self.stage = stage
        self.replacement_completed = replacement_completed
        state = "after" if replacement_completed else "before"
        super().__init__(
            f"could not publish canonical document {str(path)!r} at "
            f"{stage.value!r} {state} replacement"
        )


class DocumentReadError(DocumentFileError):
    """A failed bounded, strict, canonical document read."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"could not read canonical document {str(path)!r}")
