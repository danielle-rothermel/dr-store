from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

from dr_serialize import (
    CANONICAL_JSON_MAX_CONTAINER_DEPTH,
    Jsonable,
)

from dr_store.core.errors import (
    AllocationError,
    DocumentDirectoryError,
    ManifestPublishError,
    ManifestReadError,
    SidecarVerificationError,
)
from dr_store.core.filesystem import validate_safe_name
from dr_store.document_directory.sidecar import (
    SidecarWriter,
)
from dr_store.document_directory.sidecar import (
    verify_sidecar as _verify_sidecar,
)
from dr_store.document_file import (
    CanonicalJsonFile,
    DocumentFileError,
    DocumentPublishError,
    DocumentReadError,
)
from dr_store.document_file.canonical_json import (
    _is_reserved_document_temp_name,
    _validate_canonical_json_file_configuration,
)

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


class DocumentDirectory:
    """One last-write-wins Manifest published by same-directory replacement.

    Sidecars are streamed beside it. Manifest publishers use independent
    temporary files; callers coordinate Sidecar writers and publication order.
    """

    def __init__(
        self,
        path: Path,
        manifest_name: str,
        *,
        manifest_max_bytes: int,
        manifest_max_depth: int = CANONICAL_JSON_MAX_CONTAINER_DEPTH,
    ) -> None:
        self._path = path.absolute()
        try:
            self._manifest = CanonicalJsonFile(
                self._path,
                manifest_name,
                max_bytes=manifest_max_bytes,
                max_depth=manifest_max_depth,
            )
        except DocumentFileError as exc:
            raise AllocationError(
                f"could not configure manifest for document directory "
                f"{str(self._path)!r}"
            ) from exc

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
        manifest_max_bytes: int,
        manifest_max_depth: int = CANONICAL_JSON_MAX_CONTAINER_DEPTH,
    ) -> DocumentDirectory:
        """Allocate ``<prefix>-<utc-timestamp>-<uuid4>`` under ``root``.

        A UUID collision raises :class:`AllocationError` without retrying.
        The caller-owned root is not flushed, so its new entry may not survive
        loss of the machine or filesystem cache.
        """
        validate_safe_name(prefix, role="prefix", error=AllocationError)
        try:
            _validate_canonical_json_file_configuration(
                manifest_name,
                max_bytes=manifest_max_bytes,
                max_depth=manifest_max_depth,
            )
        except DocumentFileError as exc:
            raise AllocationError(
                "could not configure document directory manifest"
            ) from exc
        stamp = dt.datetime.now(dt.UTC).strftime(_TIMESTAMP_FORMAT)
        path = Path(root) / f"{prefix}-{stamp}-{uuid.uuid4()}"
        try:
            path.mkdir(exist_ok=False)
        except OSError as exc:
            raise AllocationError(
                f"could not allocate document directory {str(path)!r}"
            ) from exc
        return cls(
            path,
            manifest_name,
            manifest_max_bytes=manifest_max_bytes,
            manifest_max_depth=manifest_max_depth,
        )

    def publish(self, manifest: Jsonable) -> None:
        """Replace the Manifest in the same directory with canonical JSON.

        Atomic visibility depends on filesystem same-directory replace
        semantics.
        A post-replace directory-flush failure raises even though the new
        Manifest may be visible; no rollback occurs. Flush success does not
        guarantee power-loss durability.
        """
        try:
            self._manifest.publish(manifest)
        except DocumentPublishError as exc:
            raise ManifestPublishError(
                f"could not publish manifest {str(self._manifest.path)!r}"
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
        if (
            name.casefold() == self._manifest.path.name.casefold()
            or _is_reserved_document_temp_name(name)
        ):
            raise error(
                f"sidecar name {name!r} is reserved by the manifest of "
                f"{str(self._path)!r}"
            )

    def read_manifest(self) -> Jsonable:
        """Read and verify a canonical strict-JSON Manifest."""
        try:
            return self._manifest.read()
        except DocumentReadError as exc:
            raise ManifestReadError(
                f"could not read manifest {str(self._manifest.path)!r}"
            ) from exc

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
