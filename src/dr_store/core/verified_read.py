from __future__ import annotations

import errno
import hashlib
import os
import stat
from contextlib import suppress
from typing import TYPE_CHECKING

from dr_store.content_addressing import is_content_hash
from dr_store.core.descriptor_io import (
    open_child_descriptor,
    open_directory_descriptor,
    read_bounded_child_descriptor,
)
from dr_store.core.errors import (
    RegularChildFailureReason,
    VerifiedRegularChildReadError,
)
from dr_store.core.filesystem import UnsafeNameError, check_safe_name

if TYPE_CHECKING:
    from pathlib import Path

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


def _directory_open_reason(exc: BaseException) -> RegularChildFailureReason:
    if isinstance(exc, OSError) and exc.errno == errno.ENOENT:
        return RegularChildFailureReason.MISSING
    return RegularChildFailureReason.MISMATCH


def _child_open_reason(exc: BaseException) -> RegularChildFailureReason:
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOENT:
            return RegularChildFailureReason.MISSING
        if exc.errno == errno.ELOOP:
            return RegularChildFailureReason.NOT_REGULAR
    return RegularChildFailureReason.MISMATCH


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
            f"max_bytes must be a non-negative byte count, got {max_bytes!r}",
            reason=RegularChildFailureReason.BOUNDS_EXCEEDED,
        )
    if not isinstance(expected_byte_length, int) or isinstance(
        expected_byte_length, bool
    ):
        raise TypeError("expected_byte_length must be an integer")
    if expected_byte_length < 0:
        raise VerifiedRegularChildReadError(
            "expected_byte_length must be a non-negative byte count, "
            f"got {expected_byte_length!r}",
            reason=RegularChildFailureReason.BOUNDS_EXCEEDED,
        )
    if expected_byte_length > max_bytes:
        raise VerifiedRegularChildReadError(
            f"expected_byte_length {expected_byte_length} exceeds "
            f"max_bytes {max_bytes}",
            reason=RegularChildFailureReason.BOUNDS_EXCEEDED,
        )
    if not is_content_hash(expected_sha256):
        raise VerifiedRegularChildReadError(
            "expected_sha256 must be a 64-character lowercase "
            f"hexadecimal SHA-256 digest, got {expected_sha256!r}",
            reason=RegularChildFailureReason.MISMATCH,
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
            f"missing {detail}",
            reason=RegularChildFailureReason.UNSUPPORTED_PLATFORM,
        )


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
    try:
        check_safe_name(name, role="child name")
    except UnsafeNameError as exc:
        raise VerifiedRegularChildReadError(
            exc.message,
            reason=RegularChildFailureReason.BOUNDS_EXCEEDED,
        ) from None
    _require_descriptor_support()
    directory_descriptor: int | None = None
    child_descriptor: int | None = None
    try:
        try:
            directory_descriptor = open_directory_descriptor(
                directory,
                flags=_directory_flags(),
            )
        except (NotImplementedError, OSError) as exc:
            raise VerifiedRegularChildReadError(
                f"could not open directory {str(directory)!r}",
                reason=_directory_open_reason(exc),
            ) from exc
        try:
            child_descriptor = open_child_descriptor(
                name,
                flags=_child_flags(),
                directory_descriptor=directory_descriptor,
            )
        except (NotImplementedError, OSError) as exc:
            raise VerifiedRegularChildReadError(
                f"could not read child {str(child_path)!r}",
                reason=_child_open_reason(exc),
            ) from exc
        metadata = os.fstat(child_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise VerifiedRegularChildReadError(
                f"child {str(child_path)!r} is not a regular file",
                reason=RegularChildFailureReason.NOT_REGULAR,
            )
        try:
            raw = read_bounded_child_descriptor(
                child_descriptor,
                max_bytes=max_bytes,
            )
        except (NotImplementedError, OSError) as exc:
            raise VerifiedRegularChildReadError(
                f"could not read child {str(child_path)!r}",
                reason=RegularChildFailureReason.MISMATCH,
            ) from exc
        if len(raw) > max_bytes:
            raise VerifiedRegularChildReadError(
                f"child {str(child_path)!r} exceeds the "
                f"{max_bytes}-byte read bound",
                reason=RegularChildFailureReason.BOUNDS_EXCEEDED,
            )
        actual_length = len(raw)
        if actual_length != expected_byte_length:
            raise VerifiedRegularChildReadError(
                f"child {str(child_path)!r} length mismatch: expected "
                f"{expected_byte_length} bytes, stored {actual_length}",
                reason=RegularChildFailureReason.MISMATCH,
            )
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_sha256 != expected_sha256:
            raise VerifiedRegularChildReadError(
                f"child {str(child_path)!r} hash mismatch: expected "
                f"{expected_sha256}, computed {actual_sha256}",
                reason=RegularChildFailureReason.MISMATCH,
            )
        return raw
    finally:
        if child_descriptor is not None:
            with suppress(OSError):
                os.close(child_descriptor)
        if directory_descriptor is not None:
            with suppress(OSError):
                os.close(directory_descriptor)
