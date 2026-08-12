from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from dr_serialize import canonical_json_bytes, validate_strict_json
from pydantic import ValidationError

from dr_store import (
    ArtifactBundlePublication,
    ArtifactBundleReader,
    ArtifactDescriptor,
    BundleManifest,
    BundleReadLimits,
)

if TYPE_CHECKING:
    from pathlib import Path

SHA256 = "0123456789abcdef" * 4


def _descriptor(name: str = "stdout.bin") -> ArtifactDescriptor:
    return ArtifactDescriptor(
        name=name,
        sha256=SHA256,
        byte_length=12,
    )


def _canonical_bytes(manifest: BundleManifest) -> bytes:
    return canonical_json_bytes(
        validate_strict_json(manifest.model_dump(mode="json", by_alias=True))
    )


def test_manifest_has_pinned_canonical_wire_bytes() -> None:
    manifest = BundleManifest(
        artifacts=(_descriptor(),),
        payload={"z": 2, "a": [True, None]},
    )

    assert _canonical_bytes(manifest) == (
        b'{"artifacts":[{"byte_length":12,"name":"stdout.bin",'
        b'"sha256":"0123456789abcdef0123456789abcdef0123456789abcdef'
        b'0123456789abcdef"}],"format":"dr-store-artifact-bundle-v1",'
        b'"payload":{"a":[true,null],"z":2}}'
    )
    assert manifest.model_dump(mode="json", by_alias=True).keys() == {
        "format",
        "artifacts",
        "payload",
    }
    assert manifest.model_dump(mode="json", by_alias=True)["artifacts"][
        0
    ].keys() == {"name", "sha256", "byte_length"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "artifact", "sha256": "A" * 64, "byte_length": 1},
        {"name": "artifact", "sha256": "0" * 63, "byte_length": 1},
        {"name": "artifact", "sha256": "0" * 64, "byte_length": -1},
        {"name": "artifact", "sha256": "0" * 64, "byte_length": True},
        {
            "name": "artifact",
            "sha256": "0" * 64,
            "byte_length": 1,
            "unknown": "field",
        },
    ],
)
def test_descriptor_is_strict_and_closed(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ArtifactDescriptor.model_validate(arguments)


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "/absolute",
        "nested/name",
        "nested\\name",
        "nul\x00name",
        "manifest.json",
        ".dr-store-artifact-bundle-owned",
    ],
)
def test_descriptor_rejects_unsafe_and_reserved_names(name: str) -> None:
    with pytest.raises(ValidationError):
        _descriptor(name)


def test_descriptor_identity_is_exact_without_casefolding() -> None:
    assert _descriptor("MANIFEST.JSON").name == "MANIFEST.JSON"
    assert (
        _descriptor(".DR-STORE-ARTIFACT-BUNDLE-owned").name
        == ".DR-STORE-ARTIFACT-BUNDLE-owned"
    )


def test_manifest_rejects_unknown_format_unsorted_and_duplicate_names() -> (
    None
):
    first = _descriptor("a")
    second = _descriptor("b")
    with pytest.raises(ValidationError):
        BundleManifest.model_validate(
            {"format": "unknown", "artifacts": (), "payload": None}
        )
    with pytest.raises(ValidationError):
        BundleManifest(artifacts=(second, first), payload=None)
    with pytest.raises(ValidationError):
        BundleManifest(artifacts=(first, first), payload=None)
    with pytest.raises(ValidationError):
        BundleManifest.model_validate(
            {
                "format": "dr-store-artifact-bundle-v1",
                "artifacts": (),
                "payload": None,
                "unknown": "field",
            }
        )
    with pytest.raises(ValidationError):
        BundleManifest(
            artifacts=(),
            payload=("not", "json"),  # ty: ignore[invalid-argument-type]
        )


def test_models_are_frozen() -> None:
    descriptor = _descriptor()
    manifest = BundleManifest(artifacts=(descriptor,), payload=None)
    with pytest.raises(ValidationError):
        descriptor.name = "changed"  # ty: ignore[invalid-assignment]
    with pytest.raises(ValidationError):
        manifest.payload = 2  # ty: ignore[invalid-assignment]


def test_published_manifest_bytes_are_pinned_on_disk(tmp_path: Path) -> None:
    """Pin the exact stored manifest literals, not just in-memory encoding."""
    publication = ArtifactBundlePublication.allocate(tmp_path, prefix="run")
    for name, content in (("stdout.bin", b"output"), ("a.bin", b"a")):
        writer = publication.open_artifact(name)
        writer.write(content)
        writer.finalize()
    publication.publish({"z": 2, "a": [True, None]})

    assert (publication.path / "manifest.json").read_bytes() == (
        b'{"artifacts":[{"byte_length":1,"name":"a.bin",'
        b'"sha256":"ca978112ca1bbdcafac231b39a23dc4da786eff8'
        b'147c4e72b9807785afee48bb"},'
        b'{"byte_length":6,"name":"stdout.bin",'
        b'"sha256":"e0ee8bb50685e05fa0f47ed04203ae953fdfd055'
        b'f5bd2892ea186504254f8c3a"}],'
        b'"format":"dr-store-artifact-bundle-v1",'
        b'"payload":{"a":[true,null],"z":2}}'
    )


def test_bundle_recorded_by_0_2_0_reads_unchanged(tmp_path: Path) -> None:
    """Read a manifest authored byte-for-byte in the 0.2.0 wire format."""
    bundle = tmp_path / "recorded-bundle"
    bundle.mkdir()
    (bundle / "stdout.bin").write_bytes(b"output")
    (bundle / "manifest.json").write_bytes(
        b'{"artifacts":[{"byte_length":6,"name":"stdout.bin",'
        b'"sha256":"e0ee8bb50685e05fa0f47ed04203ae953fdfd055'
        b'f5bd2892ea186504254f8c3a"}],'
        b'"format":"dr-store-artifact-bundle-v1",'
        b'"payload":{"schema_version":1}}'
    )

    manifest = ArtifactBundleReader(
        bundle,
        limits=BundleReadLimits(
            manifest_max_bytes=1 << 20,
            manifest_max_depth=64,
            max_artifacts=16,
            max_bytes_per_artifact=1 << 20,
            max_total_artifact_bytes=4 << 20,
        ),
    ).audit()

    assert manifest.format == "dr-store-artifact-bundle-v1"
    assert manifest.payload == {"schema_version": 1}
    assert [item.name for item in manifest.artifacts] == ["stdout.bin"]
    assert manifest.artifacts[0].byte_length == 6
