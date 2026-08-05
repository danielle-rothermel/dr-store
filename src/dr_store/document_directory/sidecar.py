"""Streamed binary Sidecar writing, retention, and verification."""

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
    """Reject a descriptor whose inspected target is not a regular file."""
    if not stat.S_ISREG(metadata.st_mode):
        raise SidecarVerificationError(
            f"sidecar {str(path)!r} is not a regular file"
        )


def _validate_cap(cap: int | None, *, role: str) -> None:
    """Raise unless ``cap`` is an unset or non-negative byte count.

    A negative cap has no truncation meaning: it would evict more bytes than
    the stream offered, so a summary could report more ``dropped`` than
    ``produced``.
    """
    if cap is not None and cap < 0:
        raise AllocationError(
            f"{role} must be a non-negative byte count, got {cap!r}"
        )


@dataclass(frozen=True, slots=True)
class SidecarSummary:
    """What one finalized Sidecar stored, and what it discarded.

    ``head_length`` and ``tail_length`` are the stored segment lengths in
    bytes, in file order (head segment then tail segment). ``produced`` is the
    total number of bytes offered to the writer and ``dropped`` the number the
    caps discarded, so ``produced == head_length + tail_length + dropped``.
    ``digest`` is the Sidecar Digest: the full 64-character lowercase SHA-256
    of the stored bytes.

    dr-store never serializes a summary; callers project it into their own
    models and extract read-back expectations from there.
    """

    head_length: int
    tail_length: int
    produced: int
    dropped: int
    digest: str


class SidecarWriter:
    """Push-style writer for one Sidecar, owning truncation mechanics.

    The caller owns only the cap values. ``head_cap`` bytes fill first; a ring
    buffer keeps the last ``tail_cap`` bytes of everything after that, and the
    stored file is the head segment followed by the tail segment. No caps is
    unbounded: an unbounded ``head_cap`` streams every byte to the head
    segment, so nothing ever reaches the tail. ``tail_cap=0`` is head-only, and
    so is an unset ``tail_cap`` under a finite ``head_cap``: the tail buffer is
    bounded by ``tail_cap``, never by the stream. A negative cap is rejected
    before the Sidecar file is opened, so the accounting a summary reports
    holds for every writer that exists.

    A :class:`SidecarSummary` exists only after :meth:`finalize`, so a Manifest
    embedding a Sidecar Digest structurally cannot precede the Sidecar's flush.
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
        """Offer bytes to the Sidecar, applying the caps."""
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
        """Append ``part`` to the head segment on disk and to the digest."""
        try:
            self._handle.write(part)
        except (OSError, ValueError) as exc:
            raise AllocationError(
                f"could not write sidecar {str(self._path)!r}"
            ) from exc
        self._digest.update(part)
        self._head_length += len(part)

    def finalize(self) -> SidecarSummary:
        """Append the tail segment, flush durably, and return the summary."""
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
    """Verify a regular direct child through one pinned descriptor.

    This path requires directory-relative ``os.open`` plus the platform flags
    needed to refuse final symlinks for both the directory authority and its
    child, avoid blocking on special files, keep descriptors out of child
    processes, and require the directory itself.
    There is deliberately no path-based fallback because any precheck followed
    by a normal open would reintroduce a name-resolution race.
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
