from __future__ import annotations

from enum import UNIQUE, StrEnum, verify
from pathlib import Path  # noqa: TC003 - public hints resolve at runtime.


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


@verify(UNIQUE)
class ReplacementState(StrEnum):
    """The known replacement outcome of a document publication.

    Members describe reporting outcomes. Publication behavior must never be
    constructed by iterating this enum.
    """

    NOT_REPLACED = "not_replaced"
    REPLACED = "replaced"
    UNKNOWN = "unknown"


class DocumentFileError(Exception):
    """Base for standalone canonical document-file failures."""


class DocumentPublishError(DocumentFileError):
    """A failed publication with explicit phase and replacement state."""

    def __init__(
        self,
        path: Path,
        stage: PublicationStage,
        *,
        replacement_state: ReplacementState,
    ) -> None:
        self.path = path
        self.stage = stage
        self.replacement_state = replacement_state
        if replacement_state is ReplacementState.NOT_REPLACED:
            state = "without replacing the target"
        elif replacement_state is ReplacementState.REPLACED:
            state = "after replacing the target"
        else:
            state = "with an unknown replacement outcome"
        super().__init__(
            f"could not publish canonical document {str(path)!r} at "
            f"{stage.value!r} {state}"
        )


class DocumentReadError(DocumentFileError):
    """A failed bounded, strict, canonical document read."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"could not read canonical document {str(path)!r}")
