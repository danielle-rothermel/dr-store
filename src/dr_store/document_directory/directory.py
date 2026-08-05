"""The Document Directory: one atomic Manifest plus streamed Sidecars.

A Document Directory is one allocated directory with exactly one writer, one
atomically-replaced canonical-JSON Manifest, and zero or more Sidecars --
raw-bytes artifacts written incrementally beside the Manifest. The Manifest is
the source of truth about every Sidecar; a Sidecar is never self-describing.

The component is deliberately domain-neutral and narrow. It never knows
lifecycle state names or transition legality (:meth:`DocumentDirectory.publish`
is last-write-wins), never reads a field out of the Manifest payload, never
computes a retention policy, and never owns threads or child processes.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import uuid
from pathlib import Path

from dr_serialize import (
    Jsonable,
    StrictJsonError,
    canonical_json,
    validate_strict_json,
)

from dr_store.core.errors import (
    AllocationError,
    ManifestPublishError,
    ManifestReadError,
)
from dr_store.core.filesystem import (
    flush_descriptor,
    flush_directory,
    validate_safe_name,
)
from dr_store.document_directory.sidecar import (
    SidecarWriter,
)
from dr_store.document_directory.sidecar import (
    verify_sidecar as _verify_sidecar,
)

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_TEMP_SUFFIX = ".tmp"


class DocumentDirectory:
    """One allocated directory holding one Manifest and its Sidecars.

    :meth:`allocate` creates the directory and returns the instance naming it.
    The two read paths, :meth:`read_manifest` and :meth:`verify_sidecar`, are
    class methods that need no allocation. Every :meth:`publish` is a durable
    atomic replace and is last-write-wins: the component owns no lifecycle
    state and never inspects the payload it stores.
    """

    def __init__(self, path: Path, manifest_name: str) -> None:
        validate_safe_name(manifest_name, role="manifest_name")
        self._path = path
        self._manifest_name = manifest_name
        self._manifest_path = path / manifest_name
        self._temp_path = path / f"{manifest_name}{_TEMP_SUFFIX}"

    @property
    def path(self) -> Path:
        """Filesystem path of the allocated directory."""
        return self._path

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
        typed :class:`~dr_store.core.errors.AllocationError`, never a silent
        reuse and never a retry loop. ``root`` is caller-owned and must already
        exist. ``prefix`` and ``manifest_name`` are validated safe single path
        segments before anything touches disk.
        """
        validate_safe_name(prefix, role="prefix")
        validate_safe_name(manifest_name, role="manifest_name")
        stamp = dt.datetime.now(dt.UTC).strftime(_TIMESTAMP_FORMAT)
        path = Path(root) / f"{prefix}-{stamp}-{uuid.uuid4()}"
        try:
            path.mkdir(exist_ok=False)
        except OSError as exc:
            raise AllocationError(
                f"could not allocate document directory {str(path)!r}"
            ) from exc
        return cls(path, manifest_name)

    def publish(self, manifest: Jsonable) -> None:
        """Durably replace the Manifest with ``manifest``, atomically.

        The complete canonical JSON is written to a temp file in the same
        directory, flushed, atomically renamed onto the Manifest name, and the
        directory entry is flushed. A reader therefore sees either no Manifest
        or one complete previously-published Manifest, never a partial one.
        """
        try:
            canonical = canonical_json(validate_strict_json(manifest))
        except (StrictJsonError, TypeError, ValueError) as exc:
            raise ManifestPublishError(
                f"manifest for {str(self._manifest_path)!r} is not strict "
                "finite JSON"
            ) from exc
        try:
            with self._temp_path.open("wb") as handle:
                handle.write(canonical.encode("utf-8"))
                handle.flush()
                flush_descriptor(handle.fileno())
            self._temp_path.replace(self._manifest_path)
            flush_directory(self._path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                self._temp_path.unlink(missing_ok=True)
            raise ManifestPublishError(
                f"could not publish manifest {str(self._manifest_path)!r}"
            ) from exc

    def open_sidecar(
        self,
        name: str,
        *,
        head_cap: int | None = None,
        tail_cap: int | None = None,
    ) -> SidecarWriter:
        """Open a Sidecar for incremental writing beside the Manifest."""
        validate_safe_name(name, role="sidecar name")
        if name.casefold() in {
            self._manifest_name.casefold(),
            self._temp_path.name.casefold(),
        }:
            raise AllocationError(
                f"sidecar name {name!r} is reserved by the manifest of "
                f"{str(self._path)!r}"
            )
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
        """Read the Manifest of the directory at ``path``, verified."""
        validate_safe_name(
            manifest_name,
            role="manifest_name",
            error=ManifestReadError,
        )
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
        """Check a Sidecar file at ``path`` against caller expectations."""
        _verify_sidecar(
            path,
            expected_digest=expected_digest,
            expected_head_length=expected_head_length,
            expected_tail_length=expected_tail_length,
        )
