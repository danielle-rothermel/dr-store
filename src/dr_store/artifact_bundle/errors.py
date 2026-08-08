from __future__ import annotations

from enum import UNIQUE, StrEnum, verify
from pathlib import Path  # noqa: TC003 - public hints resolve at runtime.

from dr_store.document_file import ReplacementState  # noqa: TC001


@verify(UNIQUE)
class BundlePublicationPhase(StrEnum):
    """The phase in which bundle publication was refused or failed.

    Members describe reporting phases. Publication behavior must never be
    constructed by iterating this enum.
    """

    PRECONDITION = "precondition"
    ENCODE_MANIFEST = "encode_manifest"
    CREATE_TEMP = "create_temp"
    WRITE_TEMP = "write_temp"
    CLOSE_TEMP = "close_temp"
    REPLACE_MANIFEST = "replace_manifest"


class ArtifactBundleError(Exception):
    """Base for artifact-bundle publication failures."""


class BundleAllocationError(ArtifactBundleError):
    """A fresh allocation or artifact-writer admission failure."""


class BundlePublishError(ArtifactBundleError):
    """A manifest-publication refusal or terminal attempt failure."""

    def __init__(
        self,
        path: Path,
        phase: BundlePublicationPhase,
        *,
        replacement_state: ReplacementState | None,
        detail: str,
    ) -> None:
        self.path = path
        self.phase = phase
        self.replacement_state = replacement_state
        super().__init__(
            f"could not publish artifact bundle {str(path)!r} at "
            f"{phase.value!r}: {detail}"
        )
