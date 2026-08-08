from __future__ import annotations

import errno
import hashlib
import os
import stat
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dr_serialize import (
    Jsonable,
    SerializationError,
    canonical_json_bytes,
    decode_strict_json_bytes,
)
from pydantic import ValidationError

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

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Any

_READ_CHUNK_BYTES = 1 << 16
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())
_REQUIRED_OPEN_FLAGS = (
    "O_CLOEXEC",
    "O_DIRECTORY",
    "O_NOFOLLOW",
    "O_NONBLOCK",
)


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


def _require_descriptor_support(path: Path) -> None:
    missing = [
        flag
        for flag in _REQUIRED_OPEN_FLAGS
        if not isinstance(getattr(os, flag, None), int)
    ]
    if not _OPEN_SUPPORTS_DIR_FD:
        missing.append("os.open(dir_fd=...)")
    if missing:
        cause = OSError(
            errno.ENOTSUP,
            "descriptor-pinned artifact-bundle reads are unsupported: "
            + ", ".join(missing),
        )
        raise BundleReadError(
            path,
            detail="required descriptor support is unavailable",
        ) from cause


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _child_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@contextmanager
def _open_bundle_directory(path: Path) -> Iterator[int]:
    _require_descriptor_support(path)
    try:
        descriptor = os.open(path, _directory_flags())
    except (NotImplementedError, OSError, TypeError, ValueError) as exc:
        raise BundleReadError(
            path,
            detail="the bundle directory could not be opened",
        ) from exc

    body_failed = True
    try:
        yield descriptor
        body_failed = False
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if not body_failed:
                raise BundleReadError(
                    path,
                    detail="the bundle directory could not be closed",
                ) from exc


def _read_bounded(descriptor: int, max_bytes: int) -> bytes:
    chunks = bytearray()
    limit = max_bytes + 1
    while len(chunks) < limit:
        requested = min(_READ_CHUNK_BYTES, limit - len(chunks))
        chunk = os.read(descriptor, requested)
        if not chunk:
            break
        chunks.extend(chunk)
    if len(chunks) > max_bytes:
        raise ValueError("manifest exceeds manifest_max_bytes")
    return bytes(chunks)


def _require_regular_manifest(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(errno.EINVAL, "manifest is not a regular file")


def _require_canonical_manifest(document: Jsonable, raw: bytes) -> None:
    if canonical_json_bytes(document) != raw:
        raise ValueError("stored manifest is not in canonical form")


def _incomplete_manifest_error(path: Path) -> BundleIncompleteError:
    return BundleIncompleteError(
        path,
        detail="a valid terminal manifest is unavailable",
    )


def _load_manifest(
    path: Path,
    directory_descriptor: int,
    limits: BundleReadLimits,
) -> BundleManifest:
    try:
        manifest_descriptor = os.open(
            MANIFEST_NAME,
            _child_flags(),
            dir_fd=directory_descriptor,
        )
    except (NotImplementedError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, OSError) and exc.errno in {
            errno.ENOENT,
            errno.ELOOP,
            errno.EMLINK,
        }:
            raise _incomplete_manifest_error(path) from exc
        raise BundleReadError(
            path,
            detail="the terminal manifest could not be opened",
        ) from exc

    body_failed = True
    try:
        try:
            metadata = os.fstat(manifest_descriptor)
        except (NotImplementedError, OSError, TypeError, ValueError) as exc:
            raise BundleReadError(
                path,
                detail="the terminal manifest could not be inspected",
            ) from exc
        try:
            _require_regular_manifest(metadata)
        except OSError as exc:
            raise _incomplete_manifest_error(path) from exc

        try:
            raw = _read_bounded(
                manifest_descriptor,
                limits.manifest_max_bytes,
            )
        except ValueError as exc:
            raise _incomplete_manifest_error(path) from exc
        except (NotImplementedError, OSError, TypeError) as exc:
            raise BundleReadError(
                path,
                detail="the terminal manifest could not be read",
            ) from exc

        try:
            document = decode_strict_json_bytes(
                raw,
                max_bytes=limits.manifest_max_bytes,
                max_depth=limits.manifest_max_depth,
            )
            _require_canonical_manifest(document, raw)
            manifest = BundleManifest.model_validate_json(raw)
        except (
            SerializationError,
            TypeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise _incomplete_manifest_error(path) from exc
        body_failed = False
        return manifest
    finally:
        try:
            os.close(manifest_descriptor)
        except OSError as exc:
            if not body_failed:
                raise BundleReadError(
                    path,
                    detail="the terminal manifest could not be closed",
                ) from exc


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


def _artifact_open_error(
    path: Path,
    artifact_name: str,
    cause: OSError,
) -> BundleReadError:
    if cause.errno == errno.ENOENT:
        error: BundleReadError = BundleVerificationError(
            path,
            artifact_name,
            BundleVerificationReason.MISSING,
            detail="the declared child is absent",
        )
    elif cause.errno in {errno.ELOOP, errno.EMLINK}:
        error = BundleVerificationError(
            path,
            artifact_name,
            BundleVerificationReason.NOT_REGULAR,
            detail="the declared child is not a no-follow regular file",
        )
    else:
        error = BundleReadError(
            path,
            detail=f"artifact {artifact_name!r} could not be opened",
        )
    return error


def _inspect_artifact(
    path: Path,
    descriptor: ArtifactDescriptor,
    child_descriptor: int,
    *,
    max_bytes: int,
) -> None:
    metadata = os.fstat(child_descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise BundleVerificationError(
            path,
            descriptor.name,
            BundleVerificationReason.NOT_REGULAR,
            detail="the declared child is not a regular file",
        )
    if metadata.st_size > max_bytes:
        raise _bounds_error(
            path,
            descriptor.name,
            "the inspected child exceeds the remaining byte bound",
        )


def _open_artifact(
    path: Path,
    directory_descriptor: int,
    descriptor: ArtifactDescriptor,
    *,
    max_bytes: int,
) -> int:
    try:
        child_descriptor = os.open(
            descriptor.name,
            _child_flags(),
            dir_fd=directory_descriptor,
        )
    except (NotImplementedError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, OSError):
            raise _artifact_open_error(path, descriptor.name, exc) from exc
        raise BundleReadError(
            path,
            detail=f"artifact {descriptor.name!r} could not be opened",
        ) from exc

    try:
        _inspect_artifact(
            path,
            descriptor,
            child_descriptor,
            max_bytes=max_bytes,
        )
    except BundleReadError:
        with suppress(OSError):
            os.close(child_descriptor)
        raise
    except (NotImplementedError, OSError, TypeError, ValueError) as exc:
        with suppress(OSError):
            os.close(child_descriptor)
        raise BundleReadError(
            path,
            detail=f"artifact {descriptor.name!r} could not be inspected",
        ) from exc
    return child_descriptor


def _verify_descriptor_bytes(
    path: Path,
    descriptor: ArtifactDescriptor,
    child_descriptor: int,
    *,
    max_bytes: int,
) -> int:
    hasher = hashlib.sha256()
    actual_length = 0
    while True:
        remaining = max_bytes - actual_length
        requested = min(_READ_CHUNK_BYTES, remaining + 1)
        try:
            chunk = os.read(child_descriptor, requested)
        except (NotImplementedError, OSError, TypeError, ValueError) as exc:
            raise BundleReadError(
                path,
                detail=f"artifact {descriptor.name!r} could not be read",
            ) from exc
        if not chunk:
            break
        if len(chunk) > remaining:
            raise _bounds_error(
                path,
                descriptor.name,
                "recovered bytes exceed the configured artifact bound",
            )
        hasher.update(chunk)
        actual_length += len(chunk)

    if (
        actual_length != descriptor.byte_length
        or hasher.hexdigest() != descriptor.sha256
    ):
        raise BundleVerificationError(
            path,
            descriptor.name,
            BundleVerificationReason.MISMATCH,
            detail="recovered length or SHA-256 disagrees with the manifest",
        )
    return actual_length


def _audit_artifact(
    path: Path,
    directory_descriptor: int,
    descriptor: ArtifactDescriptor,
    *,
    max_bytes: int,
) -> int:
    child_descriptor = _open_artifact(
        path,
        directory_descriptor,
        descriptor,
        max_bytes=max_bytes,
    )
    failure: Exception | None = None
    verified_length: int | None = None
    try:
        verified_length = _verify_descriptor_bytes(
            path,
            descriptor,
            child_descriptor,
            max_bytes=max_bytes,
        )
    except BundleReadError as exc:
        failure = exc
    finally:
        try:
            os.close(child_descriptor)
        except OSError as exc:
            if failure is None:
                failure = BundleReadError(
                    path,
                    detail=f"artifact {descriptor.name!r} could not be closed",
                )
                failure.__cause__ = exc
    if failure is not None:
        raise failure
    assert verified_length is not None
    return verified_length


class VerifyingArtifactReader:
    """Read-only callback facade for one descriptor-owned artifact stream."""

    _path: Path
    _descriptor: ArtifactDescriptor
    _child_descriptor: int
    _max_bytes: int
    _hasher: Any
    _byte_length: int
    _eof: bool
    _active: bool
    _failure: BundleReadError | None

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
        child_descriptor: int,
        *,
        max_bytes: int,
    ) -> VerifyingArtifactReader:
        instance = object.__new__(cls)
        instance._path = path
        instance._descriptor = descriptor
        instance._child_descriptor = child_descriptor
        instance._max_bytes = max_bytes
        instance._hasher = hashlib.sha256()
        instance._byte_length = 0
        instance._eof = False
        instance._active = True
        instance._failure = None
        return instance

    def read(self, size: int = -1) -> bytes:
        """Read bytes while the owning synchronous callback is active."""
        if not self._active:
            raise BundleReadError(
                self._path,
                detail="the verifying artifact reader is no longer active",
            )
        if self._failure is not None:
            raise self._failure
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
        remaining = self._max_bytes - self._byte_length
        requested = min(requested, remaining + 1)
        try:
            chunk = os.read(self._child_descriptor, requested)
        except (NotImplementedError, OSError, TypeError, ValueError) as exc:
            error = BundleReadError(
                self._path,
                detail=(
                    f"artifact {self._descriptor.name!r} could not be read"
                ),
            )
            error.__cause__ = exc
            self._failure = error
            raise error from exc
        if not chunk:
            self._eof = True
            return b""
        if len(chunk) > remaining:
            error = _bounds_error(
                self._path,
                self._descriptor.name,
                "consumed bytes exceed the configured artifact bound",
            )
            self._failure = error
            raise error
        self._hasher.update(chunk)
        self._byte_length += len(chunk)
        return chunk

    def _invalidate(self) -> None:
        self._active = False

    def _owns_failure(self, failure: BundleReadError) -> bool:
        return self._failure is failure

    def _require_verified(self) -> None:
        if self._failure is not None:
            raise self._failure
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


def _consume_artifact(
    path: Path,
    directory_descriptor: int,
    descriptor: ArtifactDescriptor,
    consumer: Callable[[VerifyingArtifactReader], None],
    *,
    max_bytes: int,
) -> None:
    child_descriptor = _open_artifact(
        path,
        directory_descriptor,
        descriptor,
        max_bytes=max_bytes,
    )
    facade = VerifyingArtifactReader._create(
        path,
        descriptor,
        child_descriptor,
        max_bytes=max_bytes,
    )
    failure: Exception | None = None
    try:
        try:
            consumer(facade)
        except BundleReadError as exc:
            if facade._owns_failure(exc):
                raise
            raise BundleReadError(
                path,
                detail=f"artifact {descriptor.name!r} consumer failed",
            ) from exc
        except Exception as exc:
            raise BundleReadError(
                path,
                detail=f"artifact {descriptor.name!r} consumer failed",
            ) from exc
        finally:
            facade._invalidate()
        facade._require_verified()
    except BundleReadError as exc:
        failure = exc
    finally:
        try:
            os.close(child_descriptor)
        except OSError as exc:
            if failure is None:
                failure = BundleReadError(
                    path,
                    detail=f"artifact {descriptor.name!r} could not be closed",
                )
                failure.__cause__ = exc
    if failure is not None:
        raise failure


class ArtifactBundleReader:
    """Bounded synchronous reads of one terminal artifact bundle."""

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
        with _open_bundle_directory(self._path) as directory_descriptor:
            manifest = _load_manifest(
                self._path,
                directory_descriptor,
                self._limits,
            )
            _validate_manifest_limits(self._path, manifest, self._limits)
            verified_total = 0
            for descriptor in manifest.artifacts:
                remaining_total = (
                    self._limits.max_total_artifact_bytes - verified_total
                )
                verified_total += _audit_artifact(
                    self._path,
                    directory_descriptor,
                    descriptor,
                    max_bytes=min(
                        self._limits.max_bytes_per_artifact,
                        remaining_total,
                    ),
                )
        return manifest

    def consume_and_verify_artifact(
        self,
        name: str,
        consumer: Callable[[VerifyingArtifactReader], None],
    ) -> ArtifactDescriptor:
        """Deliver one declared artifact and verify the delivered bytes."""
        with _open_bundle_directory(self._path) as directory_descriptor:
            manifest = _load_manifest(
                self._path,
                directory_descriptor,
                self._limits,
            )
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
            _consume_artifact(
                self._path,
                directory_descriptor,
                descriptor,
                consumer,
                max_bytes=min(
                    self._limits.max_bytes_per_artifact,
                    self._limits.max_total_artifact_bytes,
                ),
            )
        return descriptor
