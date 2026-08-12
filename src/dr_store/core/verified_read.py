from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from typing import TYPE_CHECKING

from dr_store.content_addressing import is_content_hash
from dr_store.core.errors import VerifiedRegularChildReadError
from dr_store.core.filesystem import validate_safe_name

if TYPE_CHECKING:
    from pathlib import Path

_READ_CHUNK_BYTES = 1 << 16
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())
_REQUIRED_OPEN_FLAGS = (
    "O_CLOEXEC",
    "O_DIRECTORY",
    "O_NOFOLLOW",
    "O_NONBLOCK",
)


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _child_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _require_regular_file(
    metadata: os.stat_result,
    *,
    child_path: Path,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise VerifiedRegularChildReadError(
            f"child {str(child_path)!r} is not a regular file"
        )


def _validate_read_arguments(
    *,
    max_bytes: int,
    expected_byte_length: int,
    expected_sha256: str,
) -> None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
        raise TypeError("max_bytes must be an integer")
    if max_bytes < 0:
        raise VerifiedRegularChildReadError(
            f"max_bytes must be a non-negative byte count, got {max_bytes!r}"
        )
    if not isinstance(expected_byte_length, int) or isinstance(
        expected_byte_length, bool
    ):
        raise TypeError("expected_byte_length must be an integer")
    if expected_byte_length < 0:
        raise VerifiedRegularChildReadError(
            "expected_byte_length must be a non-negative byte count, "
            f"got {expected_byte_length!r}"
        )
    if expected_byte_length > max_bytes:
        raise VerifiedRegularChildReadError(
            f"expected_byte_length {expected_byte_length} exceeds "
            f"max_bytes {max_bytes}"
        )
    if not is_content_hash(expected_sha256):
        raise VerifiedRegularChildReadError(
            "expected_sha256 must be a 64-character lowercase "
            f"hexadecimal SHA-256 digest, got {expected_sha256!r}"
        )


def _require_descriptor_support() -> None:
    missing_flags = [
        flag
        for flag in _REQUIRED_OPEN_FLAGS
        if not isinstance(getattr(os, flag, None), int)
    ]
    if not _OPEN_SUPPORTS_DIR_FD or missing_flags:
        detail = ", ".join(missing_flags) or "os.open(dir_fd=...)"
        raise VerifiedRegularChildReadError(
            "descriptor-pinned no-follow child reads are unsupported: "
            f"missing {detail}"
        )


def _read_bounded_descriptor(child_descriptor: int, max_bytes: int) -> bytes:
    chunks = bytearray()
    limit = max_bytes + 1
    try:
        while len(chunks) < limit:
            requested = min(_READ_CHUNK_BYTES, limit - len(chunks))
            chunk = os.read(child_descriptor, requested)
            if not chunk:
                break
            chunks.extend(chunk)
    except (NotImplementedError, OSError) as exc:
        raise VerifiedRegularChildReadError(
            "could not read bounded child descriptor"
        ) from exc
    if len(chunks) > max_bytes:
        raise VerifiedRegularChildReadError(
            f"bounded read exceeds the {max_bytes}-byte limit"
        )
    return bytes(chunks)


def _verify_bounded_descriptor(
    child_descriptor: int,
    *,
    max_bytes: int,
    expected_byte_length: int,
    expected_sha256: str,
    child_path: Path | None = None,
) -> bytes:
    _validate_read_arguments(
        max_bytes=max_bytes,
        expected_byte_length=expected_byte_length,
        expected_sha256=expected_sha256,
    )
    raw = _read_bounded_descriptor(child_descriptor, max_bytes)
    actual_length = len(raw)
    subject = (
        f"child {str(child_path)!r}"
        if child_path is not None
        else "bounded read"
    )
    if actual_length != expected_byte_length:
        raise VerifiedRegularChildReadError(
            f"{subject} length mismatch: expected "
            f"{expected_byte_length} bytes, stored {actual_length}"
        )
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise VerifiedRegularChildReadError(
            f"{subject} hash mismatch: expected "
            f"{expected_sha256}, computed {actual_sha256}"
        )
    return raw


def read_verified_regular_child(
    directory: Path,
    name: str,
    *,
    max_bytes: int,
    expected_byte_length: int,
    expected_sha256: str,
) -> bytes:
    """Read and verify one regular direct child through pinned descriptors."""
    _validate_read_arguments(
        max_bytes=max_bytes,
        expected_byte_length=expected_byte_length,
        expected_sha256=expected_sha256,
    )
    child_path = directory / name
    validate_safe_name(
        name,
        role="child name",
        error=VerifiedRegularChildReadError,
    )
    _require_descriptor_support()
    directory_descriptor: int | None = None
    child_descriptor: int | None = None
    try:
        try:
            directory_descriptor = os.open(directory, _directory_flags())
        except (NotImplementedError, OSError) as exc:
            raise VerifiedRegularChildReadError(
                f"could not open directory {str(directory)!r}"
            ) from exc
        try:
            child_descriptor = os.open(
                name,
                _child_flags(),
                dir_fd=directory_descriptor,
            )
        except (NotImplementedError, OSError) as exc:
            raise VerifiedRegularChildReadError(
                f"could not read child {str(child_path)!r}"
            ) from exc
        metadata = os.fstat(child_descriptor)
        _require_regular_file(metadata, child_path=child_path)
        return _verify_bounded_descriptor(
            child_descriptor,
            max_bytes=max_bytes,
            expected_byte_length=expected_byte_length,
            expected_sha256=expected_sha256,
            child_path=child_path,
        )
    except VerifiedRegularChildReadError as exc:
        if str(exc).startswith("bounded read exceeds the"):
            raise VerifiedRegularChildReadError(
                f"child {str(child_path)!r} exceeds the "
                f"{max_bytes}-byte read bound"
            ) from exc
        if str(exc) == "could not read bounded child descriptor":
            raise VerifiedRegularChildReadError(
                f"could not read child {str(child_path)!r}"
            ) from exc.__cause__
        raise
    finally:
        if child_descriptor is not None:
            with suppress(OSError):
                os.close(child_descriptor)
        if directory_descriptor is not None:
            with suppress(OSError):
                os.close(directory_descriptor)
