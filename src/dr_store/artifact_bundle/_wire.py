from __future__ import annotations

from typing import Final

# Persisted literals are explicit contract values. Never derive wire keys from
# Python field names or build payloads by iterating these constants.
BUNDLE_FORMAT: Final = "dr-store-artifact-bundle-v1"
MANIFEST_NAME: Final = "manifest.json"
BUNDLE_TEMP_PREFIX: Final = ".dr-store-artifact-bundle-"

MANIFEST_FORMAT_KEY: Final = "format"
MANIFEST_ARTIFACTS_KEY: Final = "artifacts"
MANIFEST_PAYLOAD_KEY: Final = "payload"

DESCRIPTOR_NAME_KEY: Final = "name"
DESCRIPTOR_SHA256_KEY: Final = "sha256"
DESCRIPTOR_BYTE_LENGTH_KEY: Final = "byte_length"
