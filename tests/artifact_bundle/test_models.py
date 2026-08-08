from __future__ import annotations

from typing import Any

import pytest
from dr_serialize import canonical_json_bytes, validate_strict_json
from pydantic import ValidationError

from dr_store import ArtifactDescriptor, BundleManifest

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
