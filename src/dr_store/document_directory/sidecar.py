from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dr_store.core.errors import AllocationError, SidecarVerificationError
from dr_store.core.filesystem import flush_descriptor

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


def _require_regular_file(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise SidecarVerificationError(
            f"sidecar {str(path)!r} is not a regular file"
        )


def _validate_cap(cap: int | None, *, role: str) -> None:
    if cap is not None and cap < 0:
        raise AllocationError(
            f"{role} must be a non-negative byte count, got {cap!r}"
        )


@dataclass(frozen=True, slots=True)
class SidecarSummary:
    """Stored segment accounting and SHA-256 digest.

    ``produced == head_length + tail_length + dropped``; ``digest`` covers
    the stored head followed by the stored tail.
    """

    head_length: int
    tail_length: int
    produced: int
    dropped: int
    digest: str


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
        self._digest = hashlib.sha256()
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
        self._digest.update(part)
        self._head_length += len(part)

    def finalize(self) -> SidecarSummary:
        """Return the summary after flushing and closing the stored bytes."""
        tail = bytes(self._tail)
        try:
            self._handle.write(tail)
            self._handle.flush()
            flush_descriptor(self._handle.fileno())
        except (OSError, ValueError) as exc:
            raise AllocationError(
                f"could not flush sidecar {str(self._path)!r}"
            ) from exc
        finally:
            self._handle.close()
        self._digest.update(tail)
        return SidecarSummary(
            head_length=self._head_length,
            tail_length=len(tail),
            produced=self._produced,
            dropped=self._dropped,
            digest=self._digest.hexdigest(),
        )


def verify_sidecar(
    directory: Path,
    name: str,
    *,
    expected_digest: str,
    expected_head_length: int,
    expected_tail_length: int,
) -> None:
    """Verify a regular child through pinned, no-follow descriptors.

    There is no path fallback because a precheck followed by open would race
    name resolution.
    """
    sidecar_path = directory / name
    missing_flags = [
        flag
        for flag in _REQUIRED_OPEN_FLAGS
        if not isinstance(getattr(os, flag, None), int)
    ]
    if not _OPEN_SUPPORTS_DIR_FD or missing_flags:
        detail = ", ".join(missing_flags) or "os.open(dir_fd=...)"
        raise SidecarVerificationError(
            "atomic no-follow Sidecar verification is unsupported: "
            f"missing {detail}"
        )

    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    child_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    expected_length = expected_head_length + expected_tail_length
    digest = hashlib.sha256()
    actual_length = 0
    directory_descriptor: int | None = None
    child_descriptor: int | None = None
    try:
        directory_descriptor = os.open(directory, directory_flags)
        child_descriptor = os.open(
            name,
            child_flags,
            dir_fd=directory_descriptor,
        )
        metadata = os.fstat(child_descriptor)
        _require_regular_file(metadata, sidecar_path)
        while chunk := os.read(child_descriptor, _READ_CHUNK_BYTES):
            digest.update(chunk)
            actual_length += len(chunk)
    except SidecarVerificationError:
        raise
    except (NotImplementedError, OSError) as exc:
        raise SidecarVerificationError(
            f"could not read sidecar {str(sidecar_path)!r}"
        ) from exc
    finally:
        if child_descriptor is not None:
            with suppress(OSError):
                os.close(child_descriptor)
        if directory_descriptor is not None:
            with suppress(OSError):
                os.close(directory_descriptor)
    if actual_length != expected_length:
        raise SidecarVerificationError(
            f"sidecar {str(sidecar_path)!r} length mismatch: expected "
            f"{expected_length} bytes "
            f"({expected_head_length} head + {expected_tail_length} tail), "
            f"stored {actual_length}"
        )
    actual_digest = digest.hexdigest()
    if actual_digest != expected_digest:
        raise SidecarVerificationError(
            f"sidecar {str(sidecar_path)!r} digest mismatch: expected "
            f"{expected_digest}, computed {actual_digest}"
        )
