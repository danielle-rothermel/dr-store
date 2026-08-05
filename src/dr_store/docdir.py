"""The Document Directory: one atomic Manifest plus streamed Sidecars.

A Document Directory is one allocated directory with exactly one writer,
one atomically-replaced canonical-JSON Manifest, and zero or more Sidecars
-- raw-bytes artifacts written incrementally beside the Manifest. The
Manifest is the source of truth about every Sidecar; a Sidecar is never
self-describing.

The component is deliberately domain-neutral and narrow. It never knows
lifecycle state names or transition legality (:meth:`DocumentDirectory.
publish` is last-write-wins), never reads a field out of the Manifest
payload (it is an opaque ``Jsonable``), never computes a retention policy
(byte caps arrive as parameters), and never owns threads, drains file
descriptors, or manages child processes (the Sidecar API is push-style).

Concurrent allocation under one root is collision-free; each allocated
directory has exactly one writer by construction, not by locking. No
cross-process coordination is claimed.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dr_serialize import (
    Jsonable,
    StrictJsonError,
    canonical_json,
    validate_strict_json,
)

from dr_store.errors import (
    AllocationError,
    ManifestPublishError,
    ManifestReadError,
    SidecarVerificationError,
)

if TYPE_CHECKING:
    from types import TracebackType

_UNSAFE_NAME_CHARACTERS = frozenset({"/", "\\", "\x00"})
_RESERVED_NAMES = frozenset({"", ".", ".."})
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_TEMP_SUFFIX = ".tmp"
_READ_CHUNK_BYTES = 1 << 16


def _validate_safe_name(name: str, *, role: str) -> str:
    """Return ``name`` if it is a safe single path segment, else raise.

    A safe name is a non-empty string that is neither ``.`` nor ``..`` and
    contains no path separator or NUL byte, so it can only ever name a
    child of the directory it is joined to.
    """
    if not isinstance(name, str):
        raise AllocationError(f"{role} must be a string, got {name!r}")
    if name in _RESERVED_NAMES:
        raise AllocationError(f"{role} must be a safe name, got {name!r}")
    if any(char in _UNSAFE_NAME_CHARACTERS for char in name):
        raise AllocationError(
            f"{role} must be a single path segment with no separator, "
            f"got {name!r}"
        )
    return name


def _flush_descriptor(descriptor: int) -> None:
    """Force written bytes to the storage medium, not just the OS cache.

    macOS ``fsync`` only pushes to the drive's write cache, so the platform
    ladder is ``F_FULLFSYNC`` first and ``os.fsync`` as the fallback -- both
    where the fcntl command is absent and where the filesystem rejects it.
    """
    full_fsync = getattr(fcntl, "F_FULLFSYNC", None)
    if full_fsync is not None:
        try:
            fcntl.fcntl(descriptor, full_fsync)
        except OSError:
            os.fsync(descriptor)
        return
    os.fsync(descriptor)


def _flush_directory(directory: Path) -> None:
    """Flush a directory entry so a rename survives abrupt process death."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        _flush_descriptor(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class SidecarSummary:
    """What one finalized Sidecar stored, and what it discarded.

    ``head_length`` and ``tail_length`` are the stored segment lengths in
    bytes, in file order (head segment then tail segment). ``produced`` is
    the total number of bytes offered to the writer and ``dropped`` the
    number the caps discarded, so ``produced == head_length + tail_length +
    dropped`` always holds. ``digest`` is the Sidecar Digest: the full
    64-character lowercase SHA-256 of the stored bytes.

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

    The caller owns only the cap values. ``head_cap`` bytes fill first; a
    ring buffer keeps the last ``tail_cap`` bytes of everything after that,
    and the stored file is the head segment followed by the tail segment.
    ``None`` for both caps is unbounded; ``tail_cap=0`` is head-only.

    A :class:`SidecarSummary` exists only after :meth:`finalize`, so a
    Manifest embedding a Sidecar Digest structurally cannot precede the
    Sidecar's flush.
    """

    def __init__(
        self,
        path: Path,
        *,
        head_cap: int | None = None,
        tail_cap: int | None = None,
    ) -> None:
        if head_cap is not None and head_cap < 0:
            raise ValueError(f"head_cap must not be negative, got {head_cap}")
        if tail_cap is not None and tail_cap < 0:
            raise ValueError(f"tail_cap must not be negative, got {tail_cap}")
        self._path = path
        self._head_cap = head_cap
        self._tail_cap = tail_cap
        self._head_length = 0
        self._produced = 0
        self._tail = bytearray()
        self._dropped = 0
        self._digest = hashlib.sha256()
        self._summary: SidecarSummary | None = None
        self._handle = path.open("wb")

    @property
    def path(self) -> Path:
        """Filesystem path of the Sidecar file being written."""
        return self._path

    def write(self, chunk: bytes) -> None:
        """Offer bytes to the Sidecar, applying the caps.

        Bytes that fit under ``head_cap`` are appended to the head segment
        immediately; the remainder enters the tail ring buffer, evicting the
        oldest buffered bytes once ``tail_cap`` is reached. Every offered
        byte counts toward ``produced`` whether or not it is stored.
        """
        if self._summary is not None:
            raise ValueError("sidecar writer is already finalized")
        self._produced += len(chunk)
        remainder = chunk
        if self._head_cap is None:
            self._handle.write(chunk)
            self._digest.update(chunk)
            self._head_length += len(chunk)
            return
        room = self._head_cap - self._head_length
        if room > 0:
            head_part = chunk[:room]
            self._handle.write(head_part)
            self._digest.update(head_part)
            self._head_length += len(head_part)
            remainder = chunk[room:]
        if not remainder:
            return
        if self._tail_cap is None:
            self._tail.extend(remainder)
            return
        if self._tail_cap == 0:
            self._dropped += len(remainder)
            return
        self._tail.extend(remainder)
        overflow = len(self._tail) - self._tail_cap
        if overflow > 0:
            del self._tail[:overflow]
            self._dropped += overflow

    def finalize(self) -> SidecarSummary:
        """Append the tail segment, flush durably, and return the summary.

        Idempotent: repeated calls return the same summary without a second
        write or flush. The Sidecar Digest is computed over the stored bytes
        in file order -- head segment then tail segment -- and is finalized
        here.
        """
        if self._summary is not None:
            return self._summary
        tail = bytes(self._tail)
        self._handle.write(tail)
        self._digest.update(tail)
        self._handle.flush()
        _flush_descriptor(self._handle.fileno())
        self._handle.close()
        self._summary = SidecarSummary(
            head_length=self._head_length,
            tail_length=len(tail),
            produced=self._produced,
            dropped=self._dropped,
            digest=self._digest.hexdigest(),
        )
        self._tail.clear()
        return self._summary

    def __enter__(self) -> SidecarWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._summary is None:
            self.finalize()


class DocumentDirectory:
    """One allocated directory holding one Manifest and its Sidecars.

    Construct through :meth:`allocate`; the two read paths,
    :meth:`read_manifest` and :meth:`verify_sidecar`, are class methods that
    need no allocation. Every :meth:`publish` is a durable atomic replace
    and is last-write-wins: the component owns no lifecycle state and never
    inspects the payload it stores.
    """

    def __init__(self, path: Path, *, manifest_name: str) -> None:
        self._path = path
        self._manifest_name = _validate_safe_name(
            manifest_name,
            role="manifest_name",
        )

    @property
    def path(self) -> Path:
        """Filesystem path of the allocated directory."""
        return self._path

    @property
    def manifest_name(self) -> str:
        """Single-segment file name the Manifest is published under."""
        return self._manifest_name

    @property
    def manifest_path(self) -> Path:
        """Filesystem path of the published Manifest."""
        return self._path / self._manifest_name

    @classmethod
    def allocate(
        cls,
        root: str | Path,
        *,
        prefix: str,
        manifest_name: str,
    ) -> DocumentDirectory:
        """Create ``<prefix>-<utc-timestamp>-<uuid4>`` under ``root``.

        The directory is created with ``exist_ok=False`` so a collision is a
        typed :class:`~dr_store.errors.AllocationError`, never a silent
        reuse and never a retry loop; a missing ``root`` is created along
        the way. ``prefix`` and ``manifest_name`` are validated safe single
        path segments before anything touches disk.
        """
        _validate_safe_name(prefix, role="prefix")
        _validate_safe_name(manifest_name, role="manifest_name")
        stamp = dt.datetime.now(dt.UTC).strftime(_TIMESTAMP_FORMAT)
        path = Path(root) / f"{prefix}-{stamp}-{uuid.uuid4()}"
        try:
            path.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise AllocationError(
                f"could not allocate document directory {str(path)!r}"
            ) from exc
        return cls(path, manifest_name=manifest_name)

    def publish(self, manifest: Jsonable) -> None:
        """Durably replace the Manifest with ``manifest``, atomically.

        The recipe, in order: write the complete canonical JSON to a temp
        file in this same directory; flush it; atomically rename it onto
        ``manifest_name`` (``os.replace`` on the same filesystem); flush the
        directory entry. A reader therefore sees either no Manifest or one
        complete previously-published Manifest, never a partial one.

        The payload is opaque: it is validated as strict finite JSON and
        canonicalized through the single dr-serialize dialect, and no field
        is ever read out of it. Publishing is last-write-wins; the caller
        owns any state machine.
        """
        try:
            canonical = canonical_json(validate_strict_json(manifest))
        except (StrictJsonError, TypeError, ValueError) as exc:
            raise ManifestPublishError(
                f"manifest for {str(self.manifest_path)!r} is not strict "
                "finite JSON"
            ) from exc
        temp_path = self._path / f"{self._manifest_name}{_TEMP_SUFFIX}"
        try:
            with temp_path.open("wb") as handle:
                handle.write(canonical.encode("utf-8"))
                handle.flush()
                _flush_descriptor(handle.fileno())
            # Path.replace is os.replace: one same-filesystem atomic rename.
            temp_path.replace(self.manifest_path)
            _flush_directory(self._path)
        except OSError as exc:
            raise ManifestPublishError(
                f"could not publish manifest {str(self.manifest_path)!r}"
            ) from exc

    def open_sidecar(
        self,
        name: str,
        *,
        head_cap: int | None = None,
        tail_cap: int | None = None,
    ) -> SidecarWriter:
        """Open a Sidecar for incremental writing beside the Manifest.

        ``name`` is a validated safe single path segment. The caps are the
        caller's whole contribution to truncation: ``head_cap`` bytes fill
        first, a ring buffer keeps the last ``tail_cap`` bytes of the
        remainder, and ``None`` means unbounded.
        """
        _validate_safe_name(name, role="sidecar name")
        return SidecarWriter(
            self._path / name,
            head_cap=head_cap,
            tail_cap=tail_cap,
        )

    @classmethod
    def read_manifest(
        cls,
        path: str | Path,
        *,
        manifest_name: str,
    ) -> Jsonable:
        """Read the Manifest of the directory at ``path``, verified.

        The stored bytes must be strict finite JSON *and* in canonical form;
        a missing file, unreadable bytes, malformed or non-strict JSON, and
        byte-level drift from the canonical rendering all raise
        :class:`~dr_store.errors.ManifestReadError` with the originating
        error preserved as ``__cause__``.
        """
        _validate_safe_name(manifest_name, role="manifest_name")
        manifest_path = Path(path) / manifest_name
        try:
            raw = manifest_path.read_bytes()
        except OSError as exc:
            raise ManifestReadError(
                f"could not read manifest {str(manifest_path)!r}"
            ) from exc
        try:
            payload = validate_strict_json(json.loads(raw))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            StrictJsonError,
        ) as exc:
            raise ManifestReadError(
                f"manifest {str(manifest_path)!r} is not strict JSON"
            ) from exc
        if canonical_json(payload).encode("utf-8") != raw:
            raise ManifestReadError(
                f"manifest {str(manifest_path)!r} is not in canonical form"
            )
        return payload

    @classmethod
    def verify_sidecar(
        cls,
        path: str | Path,
        *,
        expected_digest: str,
        expected_head_length: int,
        expected_tail_length: int,
    ) -> None:
        """Check a Sidecar file at ``path`` against caller expectations.

        The component stays schema-blind: the caller extracts the expected
        Sidecar Digest and segment lengths from its own Manifest and passes
        them in. A total-length or digest disagreement raises
        :class:`~dr_store.errors.SidecarVerificationError`, as does a
        Sidecar that cannot be read.
        """
        sidecar_path = Path(path)
        expected_length = expected_head_length + expected_tail_length
        digest = hashlib.sha256()
        actual_length = 0
        try:
            with sidecar_path.open("rb") as stored:
                while chunk := stored.read(_READ_CHUNK_BYTES):
                    digest.update(chunk)
                    actual_length += len(chunk)
        except OSError as exc:
            raise SidecarVerificationError(
                f"could not read sidecar {str(sidecar_path)!r}"
            ) from exc
        if actual_length != expected_length:
            raise SidecarVerificationError(
                f"sidecar {str(sidecar_path)!r} length mismatch: expected "
                f"{expected_length} bytes "
                f"({expected_head_length} head + {expected_tail_length} "
                f"tail), stored {actual_length}"
            )
        actual_digest = digest.hexdigest()
        if actual_digest != expected_digest:
            raise SidecarVerificationError(
                f"sidecar {str(sidecar_path)!r} digest mismatch: expected "
                f"{expected_digest}, computed {actual_digest}"
            )
