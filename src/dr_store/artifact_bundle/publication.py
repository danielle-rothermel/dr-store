from __future__ import annotations

import errno
import hashlib
import threading
from contextlib import suppress
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from dr_serialize import (
    CANONICAL_JSON_MAX_CONTAINER_DEPTH,
    SerializationError,
    validate_strict_json,
)
from pydantic import ValidationError

from dr_store.artifact_bundle._layering import underlying_cause
from dr_store.artifact_bundle._wire import MANIFEST_NAME
from dr_store.artifact_bundle.errors import (
    ArtifactBundleError,
    BundleAllocationError,
    BundlePublicationPhase,
    BundlePublishError,
)
from dr_store.artifact_bundle.models import (
    ArtifactDescriptor,
    BundleManifest,
)
from dr_store.artifact_bundle.names import (
    validate_admissible_artifact_name,
    validate_single_segment,
)
from dr_store.core.errors import AllocationError, ManifestPublishError
from dr_store.document_directory import DocumentDirectory
from dr_store.document_file import PublicationStage, ReplacementState

if TYPE_CHECKING:
    from dr_serialize import Jsonable

# The bundle manifest carries a caller-owned payload of unpredictable size, so
# publication bounds it only by the document-directory encoder's own ceiling.
MANIFEST_PUBLICATION_MAX_BYTES = 1 << 30

# Bundle publication phases are reported from the document-directory
# publication stages that back them. Never build publication behavior by
# iterating this mapping.
_PUBLICATION_PHASE_BY_STAGE = {
    PublicationStage.ENCODE: BundlePublicationPhase.ENCODE_MANIFEST,
    PublicationStage.CREATE_TEMP: BundlePublicationPhase.CREATE_TEMP,
    PublicationStage.WRITE_TEMP: BundlePublicationPhase.WRITE_TEMP,
    PublicationStage.REPLACE_TARGET: BundlePublicationPhase.REPLACE_MANIFEST,
}


class _PublicationState(Enum):
    OPEN = auto()
    POISONED = auto()
    TERMINAL = auto()


class _WriterState(Enum):
    ACTIVE = auto()
    FINALIZED = auto()
    FAILED = auto()


def _write_all(handle: BinaryIO, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = handle.write(remaining)
        if written is None or written <= 0:
            raise OSError(errno.EIO, "bundle artifact write made no progress")
        remaining = remaining[written:]


def _open_exclusive_binary(path: Path) -> BinaryIO:
    return path.open("xb")


class BundleArtifactWriter:
    """Stream complete bytes into one exclusively created bundle child."""

    def __init__(
        self,
        publication: ArtifactBundlePublication,
        name: str,
        handle: BinaryIO,
    ) -> None:
        self._publication = publication
        self._name = name
        self._handle = handle
        self._hasher = hashlib.sha256()
        self._byte_length = 0

    def write(self, data: bytes) -> None:
        """Write and account for every supplied byte or poison publication."""
        self._publication._require_active_writer(self._name)
        try:
            _write_all(self._handle, data)
        except (OSError, TypeError, ValueError) as exc:
            with suppress(OSError, ValueError):
                self._handle.close()
            error = ArtifactBundleError(
                f"could not write bundle artifact {self._name!r}"
            )
            self._publication._writer_failed(self._name, error)
            raise error from exc
        self._hasher.update(data)
        self._byte_length += len(data)

    def finalize(self) -> ArtifactDescriptor:
        """Flush userspace buffers, close, and finalize the descriptor."""
        self._publication._require_active_writer(self._name)
        failure: Exception | None = None
        try:
            self._handle.flush()
        except (OSError, ValueError) as exc:
            failure = exc
        try:
            self._handle.close()
        except (OSError, ValueError) as exc:
            if failure is None:
                failure = exc
        if failure is not None:
            error = ArtifactBundleError(
                f"could not finalize bundle artifact {self._name!r}"
            )
            self._publication._writer_failed(self._name, error)
            raise error from failure

        descriptor = ArtifactDescriptor(
            name=self._name,
            sha256=self._hasher.hexdigest(),
            byte_length=self._byte_length,
        )
        self._publication._writer_finalized(self._name, descriptor)
        return descriptor


class ArtifactBundlePublication:
    """One fresh, terminal manifest-committed artifact publication.

    Manifest publication is layered over the Document Directory
    same-directory replacement path; this class owns only artifact-name
    admission, writer state, and the terminal transition.
    """

    _directory: DocumentDirectory
    _lock: threading.Lock
    _state: _PublicationState
    _writers: dict[str, _WriterState]
    _descriptors: dict[str, ArtifactDescriptor]
    _writer_failure: ArtifactBundleError | None

    def __init__(self) -> None:
        raise TypeError("use ArtifactBundlePublication.allocate(...)")

    @classmethod
    def allocate(
        cls,
        root: str | Path,
        *,
        prefix: str,
    ) -> ArtifactBundlePublication:
        """Create one fresh task, run, or result directory under ``root``."""
        try:
            validate_single_segment(prefix, role="bundle prefix")
        except (TypeError, ValueError) as exc:
            raise BundleAllocationError(
                f"invalid artifact-bundle prefix {prefix!r}"
            ) from exc

        root_path = Path(root).absolute()
        try:
            directory = DocumentDirectory.allocate(
                root_path,
                prefix=prefix,
                manifest_name=MANIFEST_NAME,
                manifest_max_bytes=MANIFEST_PUBLICATION_MAX_BYTES,
                manifest_max_depth=CANONICAL_JSON_MAX_CONTAINER_DEPTH,
            )
        except AllocationError as exc:
            raise BundleAllocationError(
                f"could not allocate artifact bundle under {str(root_path)!r}"
            ) from underlying_cause(exc)

        publication = object.__new__(cls)
        publication._directory = directory
        publication._lock = threading.Lock()
        publication._state = _PublicationState.OPEN
        publication._writers = {}
        publication._descriptors = {}
        publication._writer_failure = None
        return publication

    @property
    def path(self) -> Path:
        return self._directory.path

    def open_artifact(self, name: str) -> BundleArtifactWriter:
        """Reserve an exact name and exclusively create its regular child."""
        try:
            validate_admissible_artifact_name(name)
        except (TypeError, ValueError) as exc:
            raise BundleAllocationError(
                f"invalid bundle artifact name {name!r}"
            ) from exc

        with self._lock:
            self._require_open_for_admission()
            if name in self._writers:
                raise BundleAllocationError(
                    f"bundle artifact name {name!r} is already reserved"
                )
            self._writers[name] = _WriterState.ACTIVE
            try:
                handle = _open_exclusive_binary(self.path / name)
            except (OSError, TypeError, ValueError) as exc:
                error = BundleAllocationError(
                    f"could not create bundle artifact {name!r}"
                )
                self._writers[name] = _WriterState.FAILED
                self._state = _PublicationState.POISONED
                self._writer_failure = error
                raise error from exc
        return BundleArtifactWriter(self, name, handle)

    def publish(self, payload: Jsonable) -> None:
        """Perform the publication's sole terminal manifest attempt."""
        self._check_publish_preconditions()
        try:
            validated_payload = validate_strict_json(payload)
        except (SerializationError, TypeError, ValueError) as exc:
            raise BundlePublishError(
                self.path,
                BundlePublicationPhase.PRECONDITION,
                replacement_state=None,
                detail="payload is not one strict JSON value",
            ) from exc

        with self._lock:
            self._check_publish_preconditions_locked()
            descriptors = tuple(
                self._descriptors[name] for name in sorted(self._descriptors)
            )
            self._state = _PublicationState.TERMINAL

        try:
            manifest = BundleManifest(
                artifacts=descriptors,
                payload=validated_payload,
            )
            document = validate_strict_json(
                manifest.model_dump(mode="json", by_alias=True)
            )
        except (
            SerializationError,
            TypeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise BundlePublishError(
                self.path,
                BundlePublicationPhase.ENCODE_MANIFEST,
                replacement_state=ReplacementState.NOT_REPLACED,
                detail="manifest encoding failed",
            ) from exc

        self._publish_manifest(document)

    def _require_open_for_admission(self) -> None:
        if self._state is _PublicationState.POISONED:
            raise BundleAllocationError(
                "artifact bundle has a failed admitted writer"
            ) from self._writer_failure
        if self._state is _PublicationState.TERMINAL:
            raise BundleAllocationError(
                "artifact bundle publication is terminal"
            )

    def _require_active_writer(self, name: str) -> None:
        with self._lock:
            if self._writers.get(name) is not _WriterState.ACTIVE:
                raise ArtifactBundleError(
                    f"bundle artifact writer {name!r} is not active"
                )

    def _writer_failed(
        self,
        name: str,
        error: ArtifactBundleError,
    ) -> None:
        with self._lock:
            if self._writers.get(name) is _WriterState.ACTIVE:
                self._writers[name] = _WriterState.FAILED
                self._state = _PublicationState.POISONED
                self._writer_failure = error

    def _writer_finalized(
        self,
        name: str,
        descriptor: ArtifactDescriptor,
    ) -> None:
        with self._lock:
            if self._writers.get(name) is not _WriterState.ACTIVE:
                raise ArtifactBundleError(
                    f"bundle artifact writer {name!r} is not active"
                )
            self._descriptors[name] = descriptor
            self._writers[name] = _WriterState.FINALIZED

    def _check_publish_preconditions(self) -> None:
        with self._lock:
            self._check_publish_preconditions_locked()

    def _check_publish_preconditions_locked(self) -> None:
        if self._state is _PublicationState.POISONED:
            raise BundlePublishError(
                self.path,
                BundlePublicationPhase.PRECONDITION,
                replacement_state=None,
                detail="an admitted artifact writer failed",
            ) from self._writer_failure
        if self._state is _PublicationState.TERMINAL:
            raise BundlePublishError(
                self.path,
                BundlePublicationPhase.PRECONDITION,
                replacement_state=None,
                detail="publication is terminal",
            )
        if any(
            state is _WriterState.ACTIVE for state in self._writers.values()
        ):
            raise BundlePublishError(
                self.path,
                BundlePublicationPhase.PRECONDITION,
                replacement_state=None,
                detail="an admitted artifact writer is still active",
            )

    def _publish_manifest(self, document: Jsonable) -> None:
        try:
            self._directory.publish(document)
        except ManifestPublishError as exc:
            raise BundlePublishError(
                self.path,
                _PUBLICATION_PHASE_BY_STAGE[exc.stage],
                replacement_state=exc.replacement_state,
                detail="terminal manifest attempt failed",
            ) from underlying_cause(exc)
