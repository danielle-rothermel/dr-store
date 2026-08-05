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
    JsonEncodeError,
    StrictJsonError,
    canonical_json,
    validate_strict_json,
)

from dr_store.core.errors import (
    AllocationError,
    DocumentDirectoryError,
    ManifestPublishError,
    ManifestReadError,
    SidecarVerificationError,
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
    :meth:`read_manifest` reads a directory named by the caller, while
    :meth:`verify_sidecar` verifies a direct child of this instance's
    directory. Every :meth:`publish` uses an atomic replace and is
    last-write-wins: the component owns no lifecycle state and never inspects
    the payload it stores.
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

        Allocation does not flush the caller-owned root directory. The new
        directory is therefore visible after the call returns, but this method
        does not promise that its directory entry survives loss of the machine
        or filesystem cache.
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
        """Replace the Manifest with ``manifest`` atomically.

        The complete canonical JSON is written to a temp file in the same
        directory, flushed, atomically renamed onto the Manifest name, and the
        directory entry is flushed. A reader therefore sees either no Manifest
        or one complete previously-published Manifest, never a partial one.

        Replacement precedes the directory flush. If that final flush fails,
        this method raises :class:`~dr_store.core.errors.ManifestPublishError`
        even though the new Manifest may already be visible; the exception does
        not imply rollback. These flush operations do not promise power-loss
        durability; ordinary process-death visibility does not prove it.
        """
        try:
            canonical = canonical_json(validate_strict_json(manifest))
        except (
            JsonEncodeError,
            StrictJsonError,
            TypeError,
            ValueError,
        ) as exc:
            raise ManifestPublishError(
                f"manifest for {str(self._manifest_path)!r} is outside the "
                "Canonical JSON Text profile"
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
        self._validate_sidecar_name(name, error=AllocationError)
        return SidecarWriter(
            self._path / name,
            head_cap=head_cap,
            tail_cap=tail_cap,
        )

    def _validate_sidecar_name(
        self,
        name: str,
        *,
        error: type[DocumentDirectoryError],
    ) -> None:
        """Require one safe, non-Manifest direct-child name."""
        validate_safe_name(name, role="sidecar name", error=error)
        if name.casefold() in {
            self._manifest_name.casefold(),
            self._temp_path.name.casefold(),
        }:
            raise error(
                f"sidecar name {name!r} is reserved by the manifest of "
                f"{str(self._path)!r}"
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
        try:
            canonical = canonical_json(payload).encode("utf-8")
        except JsonEncodeError as exc:
            raise ManifestReadError(
                f"manifest {str(manifest_path)!r} is outside the Canonical "
                "JSON Text profile"
            ) from exc
        if canonical != raw:
            raise ManifestReadError(
                f"manifest {str(manifest_path)!r} is not in canonical form"
            )
        return payload

    def verify_sidecar(
        self,
        name: str,
        *,
        expected_digest: str,
        expected_head_length: int,
        expected_tail_length: int,
    ) -> None:
        """Verify one regular direct-child Sidecar without following it.

        ``name`` follows the same safe single-segment and reserved-name rules
        as :meth:`open_sidecar`. Verification refuses a final-component
        symlink for both this Document Directory and the named child, requires
        the child to be a regular file, and streams bounded reads from the
        exact descriptor whose file type was inspected.
        """
        self._validate_sidecar_name(name, error=SidecarVerificationError)
        _verify_sidecar(
            self._path,
            name,
            expected_digest=expected_digest,
            expected_head_length=expected_head_length,
            expected_tail_length=expected_tail_length,
        )
