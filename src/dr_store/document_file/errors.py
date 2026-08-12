from __future__ import annotations

from enum import UNIQUE, StrEnum, verify
from pathlib import Path  # noqa: TC003 - public hints resolve at runtime.

from dr_store.core.reasons import RegularChildFailureReason  # noqa: TC001


@verify(UNIQUE)
class PublicationStage(StrEnum):
    """The phase in which a document publication failed.

    Members describe reporting phases. Publication behavior must never be
    constructed by iterating this enum.
    """

    ENCODE = "encode"
    CREATE_TEMP = "create_temp"
    WRITE_TEMP = "write_temp"
    REPLACE_TARGET = "replace_target"


@verify(UNIQUE)
class ReplacementState(StrEnum):
    """The known replacement outcome of a document publication.

    Members describe reporting outcomes. Publication behavior must never be
    constructed by iterating this enum.
    """

    NOT_REPLACED = "not_replaced"
    REPLACED = "replaced"
    UNKNOWN = "unknown"


@verify(UNIQUE)
class ReadStage(StrEnum):
    """The phase in which a document read failed.

    Members describe reporting phases. Read behavior must never be constructed
    by iterating this enum.
    """

    OPEN_DIRECTORY = "open_directory"
    OPEN_CHILD = "open_child"
    READ_BYTES = "read_bytes"
    DECODE = "decode"
    VERIFY_CANONICALITY = "verify_canonicality"


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

    def __init__(
        self,
        path: Path,
        stage: ReadStage,
        *,
        reason: RegularChildFailureReason,
    ) -> None:
        self.path = path
        self.stage = stage
        self.reason = reason
        super().__init__(
            f"could not read canonical document {str(path)!r} at "
            f"{stage.value!r} with reason {reason.value!r}"
        )
