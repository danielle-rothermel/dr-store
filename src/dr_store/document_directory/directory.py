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
    """One last-write-wins Manifest published by same-directory replacement.

    Sidecars are streamed beside it. The caller coordinates writer exclusivity;
    this class does not enforce it.
    """

    def __init__(self, path: Path, manifest_name: str) -> None:
        validate_safe_name(manifest_name, role="manifest_name")
        self._path = path
        self._manifest_name = manifest_name
        self._manifest_path = path / manifest_name
        self._temp_path = path / f"{manifest_name}{_TEMP_SUFFIX}"

    @property
    def path(self) -> Path:
        return self._path

    @classmethod
    def allocate(
        cls,
        root: str | Path,
        *,
        prefix: str,
        manifest_name: str,
    ) -> DocumentDirectory:
        """Allocate ``<prefix>-<utc-timestamp>-<uuid4>`` under ``root``.

        A UUID collision raises :class:`AllocationError` without retrying.
        The caller-owned root is not flushed, so its new entry may not survive
        loss of the machine or filesystem cache.
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
        """Replace the Manifest in the same directory with canonical JSON.

        Atomic visibility depends on filesystem same-directory replace
        semantics.
        A post-replace directory-flush failure raises even though the new
        Manifest may be visible; no rollback occurs. Flush success does not
        guarantee power-loss durability.
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
        """Read and verify a canonical strict-JSON Manifest."""
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
        """Verify a regular child through pinned, no-follow descriptors."""
        self._validate_sidecar_name(name, error=SidecarVerificationError)
        _verify_sidecar(
            self._path,
            name,
            expected_digest=expected_digest,
            expected_head_length=expected_head_length,
            expected_tail_length=expected_tail_length,
        )
