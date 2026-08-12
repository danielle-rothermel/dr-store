from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dr_serialize import SerializationError, canonical_json_bytes
from pydantic import ValidationError

from dr_store.artifact_bundle._layering import underlying_cause
from dr_store.artifact_bundle._wire import MANIFEST_NAME
from dr_store.artifact_bundle.errors import (
    BundleIncompleteError,
    BundleReadError,
    BundleVerificationError,
    BundleVerificationReason,
)
from dr_store.artifact_bundle.models import (
    ArtifactDescriptor,
    BundleManifest,
)
from dr_store.core.errors import (
    AllocationError,
    ManifestReadError,
    RegularChildFailureReason,
    VerifiedRegularChildReadError,
)
from dr_store.core.verified_read import read_verified_regular_child
from dr_store.document_directory import DocumentDirectory
from dr_store.document_file import ReadStage

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

_READ_CHUNK_BYTES = 1 << 16

# Manifest read stages whose failures mean no valid terminal manifest exists.
# Never build read behavior by iterating this set.
_INCOMPLETE_MANIFEST_STAGES = frozenset(
    {ReadStage.DECODE, ReadStage.VERIFY_CANONICALITY}
)

# Manifest failure reasons that mean no valid terminal manifest exists.
# Never build read behavior by iterating this set.
_INCOMPLETE_MANIFEST_REASONS = frozenset(
    {
        RegularChildFailureReason.MISSING,
        RegularChildFailureReason.NOT_REGULAR,
        RegularChildFailureReason.BOUNDS_EXCEEDED,
    }
)

# Verified-read failure reasons mapped onto their bundle reporting reason.
# Never build verification behavior by iterating this mapping.
_VERIFICATION_REASON_BY_CHILD_REASON = {
    RegularChildFailureReason.MISSING: BundleVerificationReason.MISSING,
    RegularChildFailureReason.NOT_REGULAR: (
        BundleVerificationReason.NOT_REGULAR
    ),
    RegularChildFailureReason.MISMATCH: BundleVerificationReason.MISMATCH,
    RegularChildFailureReason.BOUNDS_EXCEEDED: (
        BundleVerificationReason.BOUNDS_EXCEEDED
    ),
}


def _validate_limit(value: int, *, role: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(
            f"{role} must be a non-negative integer, got {value!r}"
        )


@dataclass(frozen=True, slots=True)
class BundleReadLimits:
    """Caller-owned bounds for one manifest and its declared artifacts."""

    manifest_max_bytes: int
    manifest_max_depth: int
    max_artifacts: int
    max_bytes_per_artifact: int
    max_total_artifact_bytes: int

    def __post_init__(self) -> None:
        for role, value in (
            ("manifest_max_bytes", self.manifest_max_bytes),
            ("manifest_max_depth", self.manifest_max_depth),
            ("max_artifacts", self.max_artifacts),
            ("max_bytes_per_artifact", self.max_bytes_per_artifact),
            ("max_total_artifact_bytes", self.max_total_artifact_bytes),
        ):
            _validate_limit(value, role=role)


def _incomplete_manifest_error(path: Path) -> BundleIncompleteError:
    return BundleIncompleteError(
        path,
        detail="a valid terminal manifest is unavailable",
    )


def _bounds_error(
    path: Path,
    artifact_name: str,
    detail: str,
) -> BundleVerificationError:
    return BundleVerificationError(
        path,
        artifact_name,
        BundleVerificationReason.BOUNDS_EXCEEDED,
        detail=detail,
    )


def _manifest_failure_is_incomplete(error: ManifestReadError) -> bool:
    return (
        error.stage in _INCOMPLETE_MANIFEST_STAGES
        or error.reason in _INCOMPLETE_MANIFEST_REASONS
    )


def _load_manifest(path: Path, limits: BundleReadLimits) -> BundleManifest:
    """Read one bounded canonical manifest through the Document Directory."""
    try:
        directory = DocumentDirectory(
            path,
            MANIFEST_NAME,
            manifest_max_bytes=limits.manifest_max_bytes,
            manifest_max_depth=limits.manifest_max_depth,
        )
    except AllocationError as exc:
        raise BundleReadError(
            path,
            detail="the bundle directory could not be opened",
        ) from underlying_cause(exc)

    try:
        document = directory.read_manifest()
    except ManifestReadError as exc:
        cause = underlying_cause(exc)
        if _manifest_failure_is_incomplete(exc):
            raise _incomplete_manifest_error(path) from cause
        raise BundleReadError(
            path,
            detail="the terminal manifest could not be read",
        ) from cause

    # read_manifest already required canonical stored bytes, so re-encoding
    # reproduces them exactly and keeps JSON-shaped strict model validation.
    try:
        return BundleManifest.model_validate_json(
            canonical_json_bytes(document)
        )
    except (
        SerializationError,
        TypeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise _incomplete_manifest_error(path) from exc


def _validate_manifest_limits(
    path: Path,
    manifest: BundleManifest,
    limits: BundleReadLimits,
) -> None:
    if len(manifest.artifacts) > limits.max_artifacts:
        first_excess = manifest.artifacts[limits.max_artifacts]
        raise _bounds_error(
            path,
            first_excess.name,
            "declared artifact count exceeds max_artifacts",
        )

    declared_total = 0
    for descriptor in manifest.artifacts:
        if descriptor.byte_length > limits.max_bytes_per_artifact:
            raise _bounds_error(
                path,
                descriptor.name,
                "declared byte length exceeds max_bytes_per_artifact",
            )
        declared_total += descriptor.byte_length
        if declared_total > limits.max_total_artifact_bytes:
            raise _bounds_error(
                path,
                descriptor.name,
                "declared total exceeds max_total_artifact_bytes",
            )


def _read_verified_artifact(
    path: Path,
    descriptor: ArtifactDescriptor,
    *,
    max_bytes: int,
) -> bytes:
    """Perform one bounded verified whole-read of a declared artifact."""
    if descriptor.byte_length > max_bytes:
        raise _bounds_error(
            path,
            descriptor.name,
            "the declared child exceeds the remaining byte bound",
        )
    try:
        return read_verified_regular_child(
            path,
            descriptor.name,
            max_bytes=max_bytes,
            expected_byte_length=descriptor.byte_length,
            expected_sha256=descriptor.sha256,
        )
    except VerifiedRegularChildReadError as exc:
        reason = _VERIFICATION_REASON_BY_CHILD_REASON.get(exc.reason)
        if reason is None:
            raise BundleReadError(
                path,
                detail=f"artifact {descriptor.name!r} could not be read",
            ) from underlying_cause(exc)
        raise BundleVerificationError(
            path,
            descriptor.name,
            reason,
            detail="the declared child did not verify against the manifest",
        ) from underlying_cause(exc)


class VerifyingArtifactReader:
    """Read-only callback facade for one verified artifact's bytes."""

    _path: Path
    _descriptor: ArtifactDescriptor
    _content: bytes
    _position: int
    _hasher: Any
    _byte_length: int
    _eof: bool
    _active: bool

    def __init__(self) -> None:
        raise TypeError(
            "VerifyingArtifactReader instances are provided only during "
            "verified-consumption callbacks"
        )

    @classmethod
    def _create(
        cls,
        path: Path,
        descriptor: ArtifactDescriptor,
        content: bytes,
    ) -> VerifyingArtifactReader:
        instance = object.__new__(cls)
        instance._path = path
        instance._descriptor = descriptor
        instance._content = content
        instance._position = 0
        instance._hasher = hashlib.sha256()
        instance._byte_length = 0
        instance._eof = False
        instance._active = True
        return instance

    def read(self, size: int = -1) -> bytes:
        """Read bytes while the owning synchronous callback is active."""
        if not self._active:
            raise BundleReadError(
                self._path,
                detail="the verifying artifact reader is no longer active",
            )
        if type(size) is not int:
            raise TypeError(f"size must be an integer, got {size!r}")
        if size < -1:
            raise ValueError(f"size must be -1 or non-negative, got {size!r}")
        if size == 0:
            return b""
        if self._eof:
            return b""
        if size == -1:
            chunks = bytearray()
            while not self._eof:
                chunks.extend(self._read_once(_READ_CHUNK_BYTES))
            return bytes(chunks)
        return self._read_once(size)

    def _read_once(self, requested: int) -> bytes:
        chunk = self._content[self._position : self._position + requested]
        if not chunk:
            self._eof = True
            return b""
        self._position += len(chunk)
        self._hasher.update(chunk)
        self._byte_length += len(chunk)
        return chunk

    def _invalidate(self) -> None:
        self._active = False

    def _require_verified(self) -> None:
        if not self._eof:
            raise BundleVerificationError(
                self._path,
                self._descriptor.name,
                BundleVerificationReason.INCOMPLETE_CONSUMPTION,
                detail="the consumer returned before observing EOF",
            )
        if (
            self._byte_length != self._descriptor.byte_length
            or self._hasher.hexdigest() != self._descriptor.sha256
        ):
            raise BundleVerificationError(
                self._path,
                self._descriptor.name,
                BundleVerificationReason.MISMATCH,
                detail=(
                    "consumed length or SHA-256 disagrees with the manifest"
                ),
            )


def _deliver_artifact(
    path: Path,
    descriptor: ArtifactDescriptor,
    content: bytes,
    consumer: Callable[[VerifyingArtifactReader], None],
) -> None:
    facade = VerifyingArtifactReader._create(path, descriptor, content)
    try:
        consumer(facade)
    except Exception as exc:
        raise BundleReadError(
            path,
            detail=f"artifact {descriptor.name!r} consumer failed",
        ) from exc
    finally:
        facade._invalidate()
    facade._require_verified()


class ArtifactBundleReader:
    """Bounded synchronous reads of one terminal artifact bundle.

    Every declared artifact is recovered by one bounded, no-follow,
    descriptor-pinned verified whole-read of a regular direct child.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        limits: BundleReadLimits,
    ) -> None:
        if not isinstance(limits, BundleReadLimits):
            raise TypeError("limits must be a BundleReadLimits instance")
        self._path = Path(path).absolute()
        self._limits = limits

    def audit(self) -> BundleManifest:
        """Verify every declared artifact and return its pinned manifest."""
        manifest = _load_manifest(self._path, self._limits)
        _validate_manifest_limits(self._path, manifest, self._limits)
        verified_total = 0
        for descriptor in manifest.artifacts:
            remaining_total = (
                self._limits.max_total_artifact_bytes - verified_total
            )
            _read_verified_artifact(
                self._path,
                descriptor,
                max_bytes=min(
                    self._limits.max_bytes_per_artifact,
                    remaining_total,
                ),
            )
            verified_total += descriptor.byte_length
        return manifest

    def consume_and_verify_artifact(
        self,
        name: str,
        consumer: Callable[[VerifyingArtifactReader], None],
    ) -> ArtifactDescriptor:
        """Deliver one declared artifact and verify the delivered bytes.

        The artifact is recovered by one bounded verified whole-read, so its
        complete bytes are resident before ``consumer`` is invoked; artifacts
        must stay within ``max_bytes_per_artifact`` and
        ``max_total_artifact_bytes``. Consumption still succeeds only after
        the consumer observes EOF and the delivered bytes agree with the
        artifact descriptor.
        """
        manifest = _load_manifest(self._path, self._limits)
        _validate_manifest_limits(self._path, manifest, self._limits)
        descriptor = next(
            (
                candidate
                for candidate in manifest.artifacts
                if candidate.name == name
            ),
            None,
        )
        if descriptor is None:
            raise BundleVerificationError(
                self._path,
                name,
                BundleVerificationReason.MISSING,
                detail="the manifest declares no artifact with this name",
            )
        content = _read_verified_artifact(
            self._path,
            descriptor,
            max_bytes=min(
                self._limits.max_bytes_per_artifact,
                self._limits.max_total_artifact_bytes,
            ),
        )
        _deliver_artifact(self._path, descriptor, content, consumer)
        return descriptor
