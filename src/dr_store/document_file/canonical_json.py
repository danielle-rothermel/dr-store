from __future__ import annotations

import errno
import hashlib
import os
import stat
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

from dr_store.core.filesystem import flush_descriptor
from dr_store.document_file.errors import (
    DocumentFileError,
    DocumentPublishError,
    DocumentReadError,
    PublicationStage,
    ReplacementState,
)

_READ_CHUNK_BYTES = 1 << 16
_RESERVED_TEMP_PREFIX = ".dr-store-document-"
_UNSAFE_NAME_CHARACTERS = frozenset({"/", "\\", "\x00"})
_RESERVED_NAMES = frozenset({"", ".", ".."})
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())
_UNLINK_SUPPORTS_DIR_FD = os.unlink in getattr(os, "supports_dir_fd", ())
_COMMON_OPEN_FLAGS = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
_PUBLICATION_OPEN_FLAGS = ("O_CREAT", "O_EXCL", "O_WRONLY")
_READ_OPEN_FLAGS = ("O_NONBLOCK", "O_RDONLY")


def _is_reserved_document_temp_name(name: str) -> bool:
    return name.casefold().startswith(_RESERVED_TEMP_PREFIX.casefold())


def _validate_name(name: str) -> None:
    if name in _RESERVED_NAMES or any(
        character in _UNSAFE_NAME_CHARACTERS for character in name
    ):
        raise DocumentFileError(
            f"name must be one safe path segment, got {name!r}"
        )
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
    required_flags = [
        *_COMMON_OPEN_FLAGS,
        *(_PUBLICATION_OPEN_FLAGS if publication else _READ_OPEN_FLAGS),
    ]
    missing_flags = [
        flag
        for flag in required_flags
        if not isinstance(getattr(os, flag, None), int)
    ]
    missing_operations = []
    if not _OPEN_SUPPORTS_DIR_FD:
        missing_operations.append("os.open(dir_fd=...)")
    if publication and not _UNLINK_SUPPORTS_DIR_FD:
        missing_operations.append("os.unlink(dir_fd=...)")
    missing = [*missing_flags, *missing_operations]
    if missing:
        detail = ", ".join(missing)
        raise OSError(
            errno.ENOTSUP,
            f"descriptor-pinned document files are unsupported: {detail}",
        )


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _temp_flags() -> int:
    return os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _read_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


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
) -> tuple[Exception | None, bool]:
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
    directory_close_failed = False
    if directory_descriptor is not None:
        try:
            os.close(directory_descriptor)
        except OSError as exc:
            if failure is None:
                failure = exc
                directory_close_failed = True
    return failure, directory_close_failed


def _require_canonical_storage(document: Jsonable, raw: bytes) -> None:
    if canonical_json_bytes(document) != raw:
        raise ValueError("stored document is not in canonical form")


def _require_regular_file(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(errno.EINVAL, "document is not a regular file")


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
                _directory_flags(),
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

            stage = PublicationStage.FLUSH_TEMP
            flush_descriptor(temporary_descriptor)
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

            stage = PublicationStage.FLUSH_DIRECTORY
            flush_descriptor(directory_descriptor)
        except (NotImplementedError, OSError, TypeError, ValueError) as exc:
            failure = exc
        finally:
            failure, directory_close_failed = _finalize_publication_resources(
                temporary_descriptor=temporary_descriptor,
                temporary_name=temporary_name,
                owns_temporary=owns_temporary,
                directory_descriptor=directory_descriptor,
                failure=failure,
            )
            if directory_close_failed:
                stage = PublicationStage.FLUSH_DIRECTORY
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
            document = decode_strict_json_bytes(
                raw,
                max_bytes=self._max_bytes,
                max_depth=self._max_depth,
            )
            _require_canonical_storage(document, raw)
        except (
            NotImplementedError,
            OSError,
            SerializationError,
            TypeError,
            ValueError,
        ) as exc:
            raise DocumentReadError(self._path) from exc
        return document

    def _read_bytes(self) -> bytes:
        _require_descriptor_support(publication=False)
        directory_descriptor: int | None = None
        child_descriptor: int | None = None
        failure: Exception | None = None
        raw: bytes | None = None
        try:
            directory_descriptor = os.open(
                self._directory,
                _directory_flags(),
            )
            child_descriptor = os.open(
                self._name,
                _read_flags(),
                dir_fd=directory_descriptor,
            )
            metadata = os.fstat(child_descriptor)
            _require_regular_file(metadata)
            chunks = bytearray()
            limit = self._max_bytes + 1
            while len(chunks) < limit:
                requested = min(_READ_CHUNK_BYTES, limit - len(chunks))
                chunk = os.read(child_descriptor, requested)
                if not chunk:
                    break
                chunks.extend(chunk)
            raw = bytes(chunks)
        except (NotImplementedError, OSError, TypeError, ValueError) as exc:
            failure = exc
        finally:
            if child_descriptor is not None:
                descriptor_to_close = child_descriptor
                child_descriptor = None
                try:
                    os.close(descriptor_to_close)
                except OSError as exc:
                    if failure is None:
                        failure = exc
            if directory_descriptor is not None:
                descriptor_to_close = directory_descriptor
                directory_descriptor = None
                try:
                    os.close(descriptor_to_close)
                except OSError as exc:
                    if failure is None:
                        failure = exc
        if failure is not None:
            raise failure
        assert raw is not None
        return raw
