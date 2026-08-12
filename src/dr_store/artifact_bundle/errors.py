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
    # The manifest temporary descriptor is closed within the document-directory
    # write stage, so a close failure reports WRITE_TEMP. CLOSE_TEMP is a
    # public member that no path emits.
    CLOSE_TEMP = "close_temp"
    REPLACE_MANIFEST = "replace_manifest"


@verify(UNIQUE)
class BundleVerificationReason(StrEnum):
    """Why one declared artifact did not verify.

    Members describe reporting outcomes. Verification behavior must never be
    constructed by iterating this enum.
    """

    MISSING = "missing"
    NOT_REGULAR = "not_regular"
    MISMATCH = "mismatch"
    BOUNDS_EXCEEDED = "bounds_exceeded"
    INCOMPLETE_CONSUMPTION = "incomplete_consumption"


class ArtifactBundleError(Exception):
    """Base for artifact-bundle failures."""


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


class BundleReadError(ArtifactBundleError):
    """A manifest, filesystem, or consumer failure during a bundle read."""

    def __init__(self, path: Path, *, detail: str) -> None:
        self.path = path
        super().__init__(
            f"could not read artifact bundle {str(path)!r}: {detail}"
        )


class BundleIncompleteError(BundleReadError):
    """The bundle has no valid terminal manifest under the read limits."""


class BundleVerificationError(BundleReadError):
    """One selected or declared artifact did not verify."""

    def __init__(
        self,
        path: Path,
        artifact_name: str,
        reason: BundleVerificationReason,
        *,
        detail: str,
    ) -> None:
        self.artifact_name = artifact_name
        self.reason = reason
        super().__init__(
            path,
            detail=(
                f"artifact {artifact_name!r} failed "
                f"{reason.value!r} verification: {detail}"
            ),
        )
