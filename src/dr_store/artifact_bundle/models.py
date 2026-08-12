from __future__ import annotations

from typing import Annotated, Literal

from dr_serialize import Jsonable  # noqa: TC002 - public runtime hint.
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from dr_store.artifact_bundle._wire import (
    BUNDLE_FORMAT,
    DESCRIPTOR_BYTE_LENGTH_KEY,
    DESCRIPTOR_NAME_KEY,
    DESCRIPTOR_SHA256_KEY,
    MANIFEST_ARTIFACTS_KEY,
    MANIFEST_FORMAT_KEY,
    MANIFEST_PAYLOAD_KEY,
)
from dr_store.artifact_bundle.names import validate_artifact_name

_Sha256 = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]
_ByteLength = Annotated[int, Field(strict=True, ge=0)]


class ArtifactDescriptor(BaseModel):
    """The exact name, SHA-256 hash, and length of one bundle artifact."""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        validate_default=True,
    )

    name: str = Field(alias=DESCRIPTOR_NAME_KEY, strict=True)
    sha256: _Sha256 = Field(alias=DESCRIPTOR_SHA256_KEY)
    byte_length: _ByteLength = Field(alias=DESCRIPTOR_BYTE_LENGTH_KEY)

    @field_validator("name")
    @classmethod
    def _name_is_safe(cls, name: str) -> str:
        validate_artifact_name(name)
        return name


class BundleManifest(BaseModel):
    """The closed bundle envelope with caller-owned strict-JSON payload."""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        validate_default=True,
    )

    # The Literal repeats the explicit wire constant so static validation also
    # rejects unsupported format markers; golden bytes pin their agreement.
    format: Literal["dr-store-artifact-bundle-v1"] = Field(
        default=BUNDLE_FORMAT,
        alias=MANIFEST_FORMAT_KEY,
    )
    artifacts: tuple[ArtifactDescriptor, ...] = Field(
        alias=MANIFEST_ARTIFACTS_KEY,
    )
    payload: Jsonable = Field(alias=MANIFEST_PAYLOAD_KEY)

    @field_validator("artifacts")
    @classmethod
    def _artifacts_are_unique_and_ordered(
        cls,
        artifacts: tuple[ArtifactDescriptor, ...],
    ) -> tuple[ArtifactDescriptor, ...]:
        names = tuple(descriptor.name for descriptor in artifacts)
        if len(names) != len(set(names)):
            raise ValueError("artifact descriptor names must be unique")
        if names != tuple(sorted(names)):
            raise ValueError(
                "artifact descriptors must be ordered by exact name"
            )
        return artifacts
