from __future__ import annotations

import errno
import hashlib
import os
import uuid
from contextlib import suppress
from pathlib import Path

from dr_serialize import (
    CANONICAL_JSON_MAX_CONTAINER_DEPTH,
    Jsonable,
    SerializationError,
    canonical_json_bytes,
    decode_strict_json_bytes,
    validate_strict_json,
)
from dr_serialize.decoding.errors import (
    JsonByteLimitError,
    JsonDepthLimitError,
)

from dr_store.core.descriptor_io import (
    open_child_descriptor,
    open_directory_descriptor,
    read_bounded_child_descriptor,
)
from dr_store.core.filesystem import (
    _READ_CHUNK_BYTES,
    _child_read_open_flags,
    _directory_open_flags,
    _pinned_read_support_detail,
    validate_safe_name,
)
from dr_store.core.filesystem import (
    _require_regular_file as _require_regular_file_metadata,
)
from dr_store.document_file.errors import (
    DocumentFileError,
    DocumentPublishError,
    DocumentReadError,
    PublicationStage,
    ReadReason,
    ReadStage,
    ReplacementState,
)

_RESERVED_TEMP_PREFIX = ".dr-store-document-"
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())
_UNLINK_SUPPORTS_DIR_FD = os.unlink in getattr(os, "supports_dir_fd", ())
_COMMON_OPEN_FLAGS = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
_PUBLICATION_OPEN_FLAGS = ("O_CREAT", "O_EXCL", "O_WRONLY")


def _is_reserved_document_temp_name(name: str) -> bool:
    return name.casefold().startswith(_RESERVED_TEMP_PREFIX.casefold())


def _validate_name(name: str) -> None:
    validate_safe_name(name, role="name", error=DocumentFileError)
    if _is_reserved_document_temp_name(name):
        raise DocumentFileError(
            f"name {name!r} belongs to the reserved publication namespace"
        )


def _validate_limit(value: int, *, role: str) -> None:
    if type(value) is not int or value < 0:
        raise DocumentFileError(
            f"{role} must be a non-negative integer, got {value!r}"
        )


def _validate_canonical_json_file_configuration(
    name: str,
    *,
    max_bytes: int,
    max_depth: int,
) -> None:
    _validate_name(name)
    _validate_limit(max_bytes, role="max_bytes")
    _validate_limit(max_depth, role="max_depth")


def _require_descriptor_support(*, publication: bool) -> None:
    if not publication:
        detail = _pinned_read_support_detail()
        if detail is not None:
            raise OSError(
                errno.ENOTSUP,
                f"descriptor-pinned document files are unsupported: {detail}",
            )
        return
    required_flags = [
        *_COMMON_OPEN_FLAGS,
        *_PUBLICATION_OPEN_FLAGS,
    ]
    missing_flags = [
        flag
        for flag in required_flags
        if not isinstance(getattr(os, flag, None), int)
    ]
    missing_operations = []
    if not _OPEN_SUPPORTS_DIR_FD:
        missing_operations.append("os.open(dir_fd=...)")
    if not _UNLINK_SUPPORTS_DIR_FD:
        missing_operations.append("os.unlink(dir_fd=...)")
    missing = [*missing_flags, *missing_operations]
    if missing:
        detail = ", ".join(missing)
        raise OSError(
            errno.ENOTSUP,
            f"descriptor-pinned document files are unsupported: {detail}",
        )


def _temp_flags() -> int:
    return os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _temporary_name(target_name: str) -> str:
    target_key = hashlib.sha256(target_name.encode()).hexdigest()[:16]
    return f"{_RESERVED_TEMP_PREFIX}{target_key}-{uuid.uuid4().hex}.tmp"


def _replace(
    source: str,
    target: str,
    *,
    directory_descriptor: int,
) -> None:
    os.replace(
        source,
        target,
        src_dir_fd=directory_descriptor,
        dst_dir_fd=directory_descriptor,
    )


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "document write made no progress")
        remaining = remaining[written:]


def _finalize_publication_resources(
    *,
    temporary_descriptor: int | None,
    temporary_name: str | None,
    owns_temporary: bool,
    directory_descriptor: int | None,
    failure: Exception | None,
) -> Exception | None:
    if temporary_descriptor is not None:
        try:
            os.close(temporary_descriptor)
        except OSError as exc:
            if failure is None:
                failure = exc
    if (
        failure is not None
        and owns_temporary
        and temporary_name is not None
        and directory_descriptor is not None
    ):
        with suppress(OSError):
            os.unlink(temporary_name, dir_fd=directory_descriptor)
    if directory_descriptor is not None:
        try:
            os.close(directory_descriptor)
        except OSError as exc:
            if failure is None:
                failure = exc
    return failure


def _require_canonical_storage(document: Jsonable, raw: bytes) -> None:
    if canonical_json_bytes(document) != raw:
        raise ValueError("stored document is not in canonical form")


def _require_regular_file(metadata: os.stat_result) -> None:
    try:
        _require_regular_file_metadata(
            metadata,
            error=ValueError,
            message="document is not a regular file",
        )
    except ValueError as exc:
        raise OSError(errno.EINVAL, str(exc)) from exc


def _read_reason_from_oserror(error: OSError) -> ReadReason:
    if error.errno == errno.ENOENT:
        return ReadReason.MISSING
    if error.errno in {errno.EINVAL, errno.EBADF, errno.ELOOP}:
        message = str(error).casefold()
        if "not a regular file" in message:
            return ReadReason.NOT_REGULAR
        if error.errno == errno.ELOOP:
            return ReadReason.NOT_REGULAR
    return ReadReason.MISMATCH


def _read_reason_from_decode(error: BaseException) -> ReadReason:
    if isinstance(error, (JsonByteLimitError, JsonDepthLimitError)):
        return ReadReason.BOUNDS_EXCEEDED
    return ReadReason.MISMATCH


def _raise_read_error(
    path: Path,
    stage: ReadStage,
    error: BaseException,
) -> None:
    if isinstance(error, OSError):
        reason = _read_reason_from_oserror(error)
    else:
        reason = _read_reason_from_decode(error)
    raise DocumentReadError(path, stage, reason=reason) from error


class CanonicalJsonFile:
    """One bounded Canonical JSON Text document in an existing directory."""

    def __init__(
        self,
        directory: str | Path,
        name: str,
        *,
        max_bytes: int,
        max_depth: int = CANONICAL_JSON_MAX_CONTAINER_DEPTH,
    ) -> None:
        _validate_canonical_json_file_configuration(
            name,
            max_bytes=max_bytes,
            max_depth=max_depth,
        )
        directory_path = Path(directory).absolute()
        if not directory_path.is_dir():
            raise DocumentFileError(
                f"directory must identify an existing directory, got "
                f"{str(directory_path)!r}"
            )
        self._directory = directory_path
        self._name = name
        self._path = directory_path / name
        self._max_bytes = max_bytes
        self._max_depth = max_depth

    @property
    def path(self) -> Path:
        return self._path

    def publish(self, document: Jsonable) -> None:
        """Publish canonical bytes by one pinned same-directory replacement."""
        try:
            encoded = canonical_json_bytes(validate_strict_json(document))
            decode_strict_json_bytes(
                encoded,
                max_bytes=self._max_bytes,
                max_depth=self._max_depth,
            )
        except (SerializationError, TypeError, ValueError) as exc:
            raise DocumentPublishError(
                self._path,
                PublicationStage.ENCODE,
                replacement_state=ReplacementState.NOT_REPLACED,
            ) from exc
        self._publish_bytes(encoded)

    def _publish_bytes(self, encoded: bytes) -> None:
        stage = PublicationStage.CREATE_TEMP
        replacement_state = ReplacementState.NOT_REPLACED
        directory_descriptor: int | None = None
        temporary_descriptor: int | None = None
        temporary_name: str | None = None
        owns_temporary = False
        failure: Exception | None = None
        try:
            _require_descriptor_support(publication=True)
            directory_descriptor = os.open(
                self._directory,
                _directory_open_flags(),
            )
            temporary_name = _temporary_name(self._name)
            temporary_descriptor = os.open(
                temporary_name,
                _temp_flags(),
                0o600,
                dir_fd=directory_descriptor,
            )
            owns_temporary = True

            stage = PublicationStage.WRITE_TEMP
            _write_all(temporary_descriptor, encoded)
            descriptor_to_close = temporary_descriptor
            temporary_descriptor = None
            os.close(descriptor_to_close)

            stage = PublicationStage.REPLACE_TARGET
            replacement_state = ReplacementState.UNKNOWN
            _replace(
                temporary_name,
                self._name,
                directory_descriptor=directory_descriptor,
            )
            replacement_state = ReplacementState.REPLACED
            owns_temporary = False
        except (NotImplementedError, OSError, TypeError, ValueError) as exc:
            failure = exc
        finally:
            failure = _finalize_publication_resources(
                temporary_descriptor=temporary_descriptor,
                temporary_name=temporary_name,
                owns_temporary=owns_temporary,
                directory_descriptor=directory_descriptor,
                failure=failure,
            )
        if failure is not None:
            raise DocumentPublishError(
                self._path,
                stage,
                replacement_state=replacement_state,
            ) from failure

    def read(self) -> Jsonable:
        """Read through pinned descriptors and require canonical bytes."""
        try:
            raw = self._read_bytes()
        except DocumentReadError:
            raise
        except (NotImplementedError, OSError, TypeError, ValueError) as exc:
            _raise_read_error(self._path, ReadStage.OPEN_DIRECTORY, exc)
        stage = ReadStage.DECODE
        try:
            document = decode_strict_json_bytes(
                raw,
                max_bytes=self._max_bytes,
                max_depth=self._max_depth,
            )
            stage = ReadStage.VERIFY_CANONICALITY
            _require_canonical_storage(document, raw)
        except (
            NotImplementedError,
            OSError,
            SerializationError,
            TypeError,
            ValueError,
        ) as exc:
            _raise_read_error(self._path, stage, exc)
        return document

    def _read_bytes(self) -> bytes:
        stage = ReadStage.OPEN_DIRECTORY
        try:
            _require_descriptor_support(publication=False)
        except (NotImplementedError, OSError) as exc:
            _raise_read_error(self._path, stage, exc)
        directory_descriptor: int | None = None
        child_descriptor: int | None = None
        failure: BaseException | None = None
        failure_stage = stage
        raw: bytes | None = None
        try:
            directory_descriptor = open_directory_descriptor(
                self._directory,
                flags=_directory_open_flags(),
            )
            stage = ReadStage.OPEN_CHILD
            child_descriptor = open_child_descriptor(
                self._name,
                flags=_child_read_open_flags(),
                directory_descriptor=directory_descriptor,
            )
            stage = ReadStage.READ_BYTES
            metadata = os.fstat(child_descriptor)
            _require_regular_file(metadata)
            raw = read_bounded_child_descriptor(
                child_descriptor,
                max_bytes=self._max_bytes,
                chunk_bytes=_READ_CHUNK_BYTES,
            )
            if len(raw) > self._max_bytes:
                raise DocumentReadError(  # noqa: TRY301
                    self._path,
                    ReadStage.READ_BYTES,
                    reason=ReadReason.BOUNDS_EXCEEDED,
                )
        except DocumentReadError:
            raise
        except (NotImplementedError, OSError, TypeError, ValueError) as exc:
            failure = exc
            failure_stage = stage
        finally:
            if child_descriptor is not None:
                descriptor_to_close = child_descriptor
                child_descriptor = None
                try:
                    os.close(descriptor_to_close)
                except OSError as exc:
                    if failure is None:
                        failure = exc
                        failure_stage = stage
            if directory_descriptor is not None:
                descriptor_to_close = directory_descriptor
                directory_descriptor = None
                try:
                    os.close(descriptor_to_close)
                except OSError as exc:
                    if failure is None:
                        failure = exc
                        failure_stage = stage
        if failure is not None:
            _raise_read_error(self._path, failure_stage, failure)
        assert raw is not None
        return raw
