from __future__ import annotations

import errno
from typing import TYPE_CHECKING

import pytest

from dr_store import (
    DocumentDirectory,
    ManifestPublishError,
    ManifestReadError,
)
from dr_store.document_file import (
    DocumentPublishError,
    DocumentReadError,
    PublicationStage,
    ReplacementState,
)
from dr_store.document_file import canonical_json as file_module

if TYPE_CHECKING:
    from pathlib import Path

    from dr_serialize import Jsonable

MANIFEST_NAME = "record.json"
MANIFEST_MAX_BYTES = 1 << 20
FIRST: Jsonable = {"state": "started", "sidecars": []}
SECOND: Jsonable = {"state": "finished", "sidecars": ["stdout.bin"]}


def _allocate(
    root: Path,
    *,
    max_bytes: int = MANIFEST_MAX_BYTES,
    max_depth: int = 200,
) -> DocumentDirectory:
    return DocumentDirectory.allocate(
        root,
        prefix="run",
        manifest_name=MANIFEST_NAME,
        manifest_max_bytes=max_bytes,
        manifest_max_depth=max_depth,
    )


def test_publish_writes_canonical_bytes_without_temp_residue(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish({"b": 2, "a": 1})
    assert (directory.path / MANIFEST_NAME).read_bytes() == b'{"a":1,"b":2}'
    assert directory.read_manifest() == {"a": 1, "b": 2}
    assert [path.name for path in directory.path.iterdir()] == [MANIFEST_NAME]


def test_publish_is_last_successful_replacement_wins(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    directory.publish(SECOND)
    assert directory.read_manifest() == SECOND


def test_publish_error_is_thin_contextual_translation(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)

    with pytest.raises(ManifestPublishError) as caught:
        directory.publish({"bad": float("inf")})

    document_error = caught.value.__cause__
    assert isinstance(document_error, DocumentPublishError)
    assert document_error.stage is PublicationStage.ENCODE
    assert document_error.replacement_state is ReplacementState.NOT_REPLACED
    assert document_error.__cause__ is not None
    assert directory.read_manifest() == FIRST


def test_pre_replace_failure_preserves_manifest_and_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    failure = OSError(errno.EIO, "write failed")

    def fail_write(*_args: object) -> None:
        raise failure

    monkeypatch.setattr(file_module, "_write_all", fail_write)
    with pytest.raises(ManifestPublishError) as caught:
        directory.publish(SECOND)

    document_error = caught.value.__cause__
    assert isinstance(document_error, DocumentPublishError)
    assert document_error.stage is PublicationStage.WRITE_TEMP
    assert document_error.replacement_state is ReplacementState.NOT_REPLACED
    assert document_error.__cause__ is failure
    assert directory.read_manifest() == FIRST


def test_post_replace_failure_reports_visible_manifest_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    calls = 0
    original_flush = file_module.flush_descriptor

    def fail_directory_flush(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "directory flush failed")
        original_flush(descriptor)

    monkeypatch.setattr(
        file_module,
        "flush_descriptor",
        fail_directory_flush,
    )
    with pytest.raises(ManifestPublishError) as caught:
        directory.publish(SECOND)

    document_error = caught.value.__cause__
    assert isinstance(document_error, DocumentPublishError)
    assert document_error.stage is PublicationStage.FLUSH_DIRECTORY
    assert document_error.replacement_state is ReplacementState.REPLACED
    assert directory.read_manifest() == SECOND


@pytest.mark.parametrize(
    "raw",
    [
        b'{"truncated":',
        b'{"value":NaN}',
        b'{"duplicate":1,"duplicate":2}',
        b'{"a": 1}',
        b"\xff",
    ],
    ids=["syntax", "nonfinite", "duplicate", "noncanonical", "utf8"],
)
def test_read_error_is_thin_contextual_translation(
    tmp_path: Path,
    raw: bytes,
) -> None:
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(raw)

    with pytest.raises(ManifestReadError) as caught:
        directory.read_manifest()

    document_error = caught.value.__cause__
    assert isinstance(document_error, DocumentReadError)
    assert document_error.path == directory.path / MANIFEST_NAME
    assert document_error.__cause__ is not None


def test_manifest_publish_and_read_share_configured_byte_bound(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path, max_bytes=3)
    with pytest.raises(ManifestPublishError):
        directory.publish(None)
    (directory.path / MANIFEST_NAME).write_bytes(b"null")
    with pytest.raises(ManifestReadError):
        directory.read_manifest()


def test_manifest_publish_and_read_share_configured_depth_bound(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path, max_depth=1)
    with pytest.raises(ManifestPublishError):
        directory.publish([[None]])
    (directory.path / MANIFEST_NAME).write_bytes(b"[[null]]")
    with pytest.raises(ManifestReadError):
        directory.read_manifest()
