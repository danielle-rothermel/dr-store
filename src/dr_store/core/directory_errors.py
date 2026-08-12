from __future__ import annotations

from typing import TYPE_CHECKING

from dr_store.document_file.errors import (
    PublicationStage,
    ReadStage,
    ReplacementState,
)

if TYPE_CHECKING:
    from pathlib import Path

    from dr_store.core.reasons import RegularChildFailureReason


class DocumentDirectoryError(Exception):
    """Base for every Document Directory failure."""


class AllocationError(DocumentDirectoryError):
    """Covers Document Directory allocation, name, and cap faults.

    Also covers Sidecar open, write, and finalize failures.
    """


class ManifestPublishError(DocumentDirectoryError):
    """A failed manifest publication with explicit phase and state."""

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
            f"could not publish manifest {str(path)!r} at "
            f"{stage.value!r} {state}"
        )


class ManifestReadError(DocumentDirectoryError):
    """A failed bounded, strict, canonical manifest read."""

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
            f"could not read manifest {str(path)!r} at "
            f"{stage.value!r} with reason {reason.value!r}"
        )


class SidecarVerificationError(DocumentDirectoryError):
    def __init__(
        self,
        path: Path,
        reason: RegularChildFailureReason,
    ) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"sidecar verification failed for {str(path)!r}: {reason.value!r}"
        )
