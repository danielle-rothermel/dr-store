from __future__ import annotations

import datetime as dt
import errno
import hashlib
import threading
import uuid
from contextlib import suppress
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from dr_serialize import (
    SerializationError,
    canonical_json_bytes,
    validate_strict_json,
)
from pydantic import ValidationError

from dr_store.artifact_bundle._wire import (
    BUNDLE_TEMP_PREFIX,
    MANIFEST_NAME,
)
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
    validate_artifact_name,
    validate_single_segment,
)
from dr_store.document_file import ReplacementState

if TYPE_CHECKING:
    from dr_serialize import Jsonable

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


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


def _replace_manifest(temporary_path: Path, manifest_path: Path) -> None:
    temporary_path.replace(manifest_path)


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
    """One fresh, terminal manifest-committed artifact publication."""

    _path: Path
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
        stamp = dt.datetime.now(dt.UTC).strftime(_TIMESTAMP_FORMAT)
        path = root_path / f"{prefix}-{stamp}-{uuid.uuid4()}"
        try:
            path.mkdir(exist_ok=False)
        except OSError as exc:
            raise BundleAllocationError(
                f"could not allocate artifact bundle {str(path)!r}"
            ) from exc

        publication = object.__new__(cls)
        publication._path = path
        publication._lock = threading.Lock()
        publication._state = _PublicationState.OPEN
        publication._writers = {}
        publication._descriptors = {}
        publication._writer_failure = None
        return publication

    @property
    def path(self) -> Path:
        return self._path

    def open_artifact(self, name: str) -> BundleArtifactWriter:
        """Reserve an exact name and exclusively create its regular child."""
        try:
            validate_artifact_name(name)
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
                handle = _open_exclusive_binary(self._path / name)
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
                self._path,
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
            encoded = canonical_json_bytes(
                validate_strict_json(
                    manifest.model_dump(mode="json", by_alias=True)
                )
            )
        except (
            SerializationError,
            TypeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise BundlePublishError(
                self._path,
                BundlePublicationPhase.ENCODE_MANIFEST,
                replacement_state=ReplacementState.NOT_REPLACED,
                detail="manifest encoding failed",
            ) from exc

        self._publish_manifest(encoded)

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
                self._path,
                BundlePublicationPhase.PRECONDITION,
                replacement_state=None,
                detail="an admitted artifact writer failed",
            ) from self._writer_failure
        if self._state is _PublicationState.TERMINAL:
            raise BundlePublishError(
                self._path,
                BundlePublicationPhase.PRECONDITION,
                replacement_state=None,
                detail="publication is terminal",
            )
        if any(
            state is _WriterState.ACTIVE for state in self._writers.values()
        ):
            raise BundlePublishError(
                self._path,
                BundlePublicationPhase.PRECONDITION,
                replacement_state=None,
                detail="an admitted artifact writer is still active",
            )

    def _publish_manifest(self, encoded: bytes) -> None:
        temporary_path = self._path / (
            f"{BUNDLE_TEMP_PREFIX}manifest-{uuid.uuid4().hex}.tmp"
        )
        manifest_path = self._path / MANIFEST_NAME
        phase = BundlePublicationPhase.CREATE_TEMP
        replacement_state = ReplacementState.NOT_REPLACED
        handle: BinaryIO | None = None
        owns_temporary = False
        failure: Exception | None = None
        try:
            handle = _open_exclusive_binary(temporary_path)
            owns_temporary = True

            phase = BundlePublicationPhase.WRITE_TEMP
            _write_all(handle, encoded)

            phase = BundlePublicationPhase.CLOSE_TEMP
            handle.flush()
            handle_to_close = handle
            handle = None
            handle_to_close.close()

            phase = BundlePublicationPhase.REPLACE_MANIFEST
            replacement_state = ReplacementState.UNKNOWN
            _replace_manifest(temporary_path, manifest_path)
            replacement_state = ReplacementState.REPLACED
            owns_temporary = False
        except (OSError, TypeError, ValueError) as exc:
            failure = exc
        finally:
            if handle is not None:
                try:
                    handle.close()
                except (OSError, ValueError) as exc:
                    if failure is None:
                        phase = BundlePublicationPhase.CLOSE_TEMP
                        failure = exc
            if owns_temporary:
                with suppress(OSError):
                    temporary_path.unlink()

        if failure is not None:
            raise BundlePublishError(
                self._path,
                phase,
                replacement_state=replacement_state,
                detail="terminal manifest attempt failed",
            ) from failure
