from __future__ import annotations

import errno
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dr_store.core.errors import (
    AllocationError,
    SidecarVerificationError,
    SidecarVerificationReason,
    VerifiedRegularChildReadError,
)
from dr_store.core.verified_read import read_verified_regular_child

if TYPE_CHECKING:
    from pathlib import Path


def _validate_cap(cap: int | None, *, role: str) -> None:
    if cap is not None and cap < 0:
        raise AllocationError(
            f"{role} must be a non-negative byte count, got {cap!r}"
        )


@dataclass(frozen=True, slots=True)
class SidecarSummary:
    """Stored segment accounting and SHA-256 sidecar hash.

    ``produced == head_length + tail_length + dropped``; ``sidecar_hash``
    covers the stored head followed by the stored tail.
    """

    head_length: int
    tail_length: int
    produced: int
    dropped: int
    sidecar_hash: str


class SidecarWriter:
    """Incrementally retain capped head and tail bytes.

    An unset head cap retains the whole stream and leaves no tail. With a
    finite head, an unset or zero tail cap discards the remainder. Negative
    caps are rejected before opening the file. Finalization flushes the file
    descriptor but not its directory entry.
    """

    def __init__(
        self,
        path: Path,
        *,
        head_cap: int | None = None,
        tail_cap: int | None = None,
    ) -> None:
        _validate_cap(head_cap, role="head_cap")
        _validate_cap(tail_cap, role="tail_cap")
        self._path = path
        self._head_cap = head_cap
        self._tail_cap = 0 if tail_cap is None else tail_cap
        self._head_length = 0
        self._produced = 0
        self._tail = bytearray()
        self._dropped = 0
        self._sidecar_hasher = hashlib.sha256()
        try:
            self._handle = path.open("wb")
        except OSError as exc:
            raise AllocationError(
                f"could not open sidecar {str(path)!r}"
            ) from exc

    def write(self, chunk: bytes) -> None:
        """Offer bytes under the configured caps.

        After an error, abandon the writer: accounting may include the chunk,
        and retry or finalization has no supported result.
        """
        self._produced += len(chunk)
        remainder = chunk
        if self._head_cap is None:
            self._store_head(chunk)
            return
        room = self._head_cap - self._head_length
        if room > 0:
            self._store_head(chunk[:room])
            remainder = chunk[room:]
        if not remainder:
            return
        if self._tail_cap == 0:
            self._dropped += len(remainder)
            return
        self._tail.extend(remainder)
        overflow = len(self._tail) - self._tail_cap
        if overflow > 0:
            del self._tail[:overflow]
            self._dropped += overflow

    def _store_head(self, part: bytes) -> None:
        try:
            self._handle.write(part)
        except (OSError, ValueError) as exc:
            raise AllocationError(
                f"could not write sidecar {str(self._path)!r}"
            ) from exc
        self._sidecar_hasher.update(part)
        self._head_length += len(part)

    def finalize(self) -> SidecarSummary:
        """Return the summary after flushing and closing the stored bytes."""
        tail = bytes(self._tail)
        try:
            self._handle.write(tail)
            self._handle.flush()
        except (OSError, ValueError) as exc:
            raise AllocationError(
                f"could not flush sidecar {str(self._path)!r}"
            ) from exc
        finally:
            self._handle.close()
        self._sidecar_hasher.update(tail)
        return SidecarSummary(
            head_length=self._head_length,
            tail_length=len(tail),
            produced=self._produced,
            dropped=self._dropped,
            sidecar_hash=self._sidecar_hasher.hexdigest(),
        )


def _open_failure_reason(
    exc: BaseException,
    *,
    child_open: bool,
) -> SidecarVerificationReason:
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOENT:
            return SidecarVerificationReason.MISSING
        if child_open and exc.errno == errno.ELOOP:
            return SidecarVerificationReason.NOT_REGULAR
    return SidecarVerificationReason.MISMATCH


def verify_sidecar(
    directory: Path,
    name: str,
    *,
    expected_sidecar_hash: str,
    expected_head_length: int,
    expected_tail_length: int,
) -> None:
    """Verify a regular child through pinned, no-follow descriptors.

    There is no path fallback because a precheck followed by open would race
    name resolution.
    """
    sidecar_path = directory / name
    for _role, length in (
        ("expected_head_length", expected_head_length),
        ("expected_tail_length", expected_tail_length),
    ):
        if length < 0:
            raise SidecarVerificationError(
                sidecar_path,
                SidecarVerificationReason.BOUNDS_EXCEEDED,
            )

    expected_length = expected_head_length + expected_tail_length
    try:
        read_verified_regular_child(
            directory,
            name,
            max_bytes=expected_length,
            expected_byte_length=expected_length,
            expected_sha256=expected_sidecar_hash,
        )
    except VerifiedRegularChildReadError as exc:
        message = str(exc)
        if "exceeds the" in message:
            raise SidecarVerificationError(
                sidecar_path,
                SidecarVerificationReason.BOUNDS_EXCEEDED,
            ) from exc
        if "length mismatch" in message or "hash mismatch" in message:
            raise SidecarVerificationError(
                sidecar_path,
                SidecarVerificationReason.MISMATCH,
            ) from exc
        if "is not a regular file" in message:
            raise SidecarVerificationError(
                sidecar_path,
                SidecarVerificationReason.NOT_REGULAR,
            ) from exc
        if message.startswith("descriptor-pinned no-follow child reads"):
            raise SidecarVerificationError(
                sidecar_path,
                SidecarVerificationReason.UNSUPPORTED_PLATFORM,
            ) from None
        if exc.__cause__ is not None:
            child_open = message.startswith("could not read child ")
            raise SidecarVerificationError(
                sidecar_path,
                _open_failure_reason(exc.__cause__, child_open=child_open),
            ) from exc.__cause__
        raise SidecarVerificationError(
            sidecar_path,
            SidecarVerificationReason.MISMATCH,
        ) from exc
