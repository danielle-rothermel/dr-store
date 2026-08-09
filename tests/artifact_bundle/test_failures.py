from __future__ import annotations

import errno
from typing import TYPE_CHECKING, cast

import pytest

from dr_store import (
    ArtifactBundleError,
    ArtifactBundlePublication,
    BundleAllocationError,
    BundlePublicationPhase,
    BundlePublishError,
    ReplacementState,
)
from dr_store.artifact_bundle import publication as publication_module

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO


def _allocate(root: Path) -> ArtifactBundlePublication:
    return ArtifactBundlePublication.allocate(root, prefix="run")


def test_existing_child_create_failure_poisons_publication(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)
    (publication.path / "stdout.bin").write_bytes(b"external")

    with pytest.raises(BundleAllocationError) as admitted:
        publication.open_artifact("stdout.bin")
    assert isinstance(admitted.value.__cause__, FileExistsError)

    with pytest.raises(BundlePublishError) as caught:
        publication.publish(None)
    assert caught.value.phase is BundlePublicationPhase.PRECONDITION
    assert caught.value.replacement_state is None
    assert caught.value.__cause__ is admitted.value
    with pytest.raises(BundleAllocationError):
        publication.open_artifact("other.bin")
    assert not (publication.path / "manifest.json").exists()


class _WriteFailureHandle:
    def __init__(
        self,
        wrapped: BinaryIO,
        write_failure: OSError,
        *,
        close_failure: OSError | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._write_failure = write_failure
        self._close_failure = close_failure
        self.close_attempts = 0

    def write(self, _data: bytes | memoryview) -> int:
        raise self._write_failure

    def close(self) -> None:
        self.close_attempts += 1
        self._wrapped.close()
        if self._close_failure is not None:
            raise self._close_failure


def test_admitted_write_failure_poisons_publication_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = publication_module._open_exclusive_binary
    failure = OSError(errno.EIO, "write refused")
    handles: list[_WriteFailureHandle] = []

    def failing_open(path: Path) -> BinaryIO:
        handle = _WriteFailureHandle(original_open(path), failure)
        handles.append(handle)
        return cast("BinaryIO", handle)

    monkeypatch.setattr(
        publication_module,
        "_open_exclusive_binary",
        failing_open,
    )
    publication = _allocate(tmp_path)
    writer = publication.open_artifact("stdout.bin")

    with pytest.raises(ArtifactBundleError) as writer_error:
        writer.write(b"data")
    assert writer_error.value.__cause__ is failure
    assert handles[0].close_attempts == 1
    assert handles[0]._wrapped.closed is True

    with pytest.raises(BundlePublishError) as caught:
        publication.publish(None)
    assert caught.value.phase is BundlePublicationPhase.PRECONDITION
    assert caught.value.replacement_state is None
    assert caught.value.__cause__ is writer_error.value


def test_admitted_write_close_failure_preserves_write_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = publication_module._open_exclusive_binary
    write_failure = OSError(errno.EIO, "write refused")
    close_failure = OSError(errno.EIO, "close refused")
    handles: list[_WriteFailureHandle] = []

    def failing_open(path: Path) -> BinaryIO:
        handle = _WriteFailureHandle(
            original_open(path),
            write_failure,
            close_failure=close_failure,
        )
        handles.append(handle)
        return cast("BinaryIO", handle)

    monkeypatch.setattr(
        publication_module,
        "_open_exclusive_binary",
        failing_open,
    )
    publication = _allocate(tmp_path)
    writer = publication.open_artifact("stdout.bin")

    with pytest.raises(ArtifactBundleError) as writer_error:
        writer.write(b"data")
    assert writer_error.value.__cause__ is write_failure
    assert handles[0].close_attempts == 1
    assert handles[0]._wrapped.closed is True

    with pytest.raises(BundlePublishError) as caught:
        publication.publish(None)
    assert caught.value.__cause__ is writer_error.value


class _FinalizeFailureHandle:
    def __init__(self, wrapped: BinaryIO) -> None:
        self._wrapped = wrapped

    def write(self, data: bytes | memoryview) -> int:
        return self._wrapped.write(data)

    def flush(self) -> None:
        raise OSError(errno.EIO, "flush refused")

    def close(self) -> None:
        self._wrapped.close()


def test_admitted_finalize_failure_poisons_publication_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = publication_module._open_exclusive_binary
    handles: list[_FinalizeFailureHandle] = []

    def failing_open(path: Path) -> BinaryIO:
        handle = _FinalizeFailureHandle(original_open(path))
        handles.append(handle)
        return cast("BinaryIO", handle)

    monkeypatch.setattr(
        publication_module,
        "_open_exclusive_binary",
        failing_open,
    )
    publication = _allocate(tmp_path)
    writer = publication.open_artifact("stdout.bin")
    writer.write(b"data")

    with pytest.raises(ArtifactBundleError) as writer_error:
        writer.finalize()
    assert isinstance(writer_error.value.__cause__, OSError)
    assert handles[0]._wrapped.closed is True

    with pytest.raises(BundlePublishError) as caught:
        publication.publish(None)
    assert caught.value.__cause__ is writer_error.value


def test_terminal_manifest_write_failure_is_not_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _allocate(tmp_path)
    failure = OSError(errno.EIO, "manifest write refused")

    def fail_write(*_args: object) -> None:
        raise failure

    monkeypatch.setattr(publication_module, "_write_all", fail_write)
    with pytest.raises(BundlePublishError) as caught:
        publication.publish(None)

    assert caught.value.phase is BundlePublicationPhase.WRITE_TEMP
    assert caught.value.replacement_state is ReplacementState.NOT_REPLACED
    assert caught.value.__cause__ is failure
    assert list(publication.path.iterdir()) == []

    with pytest.raises(BundlePublishError) as retry:
        publication.publish(None)
    assert retry.value.phase is BundlePublicationPhase.PRECONDITION
    assert retry.value.replacement_state is None


def test_terminal_manifest_close_failure_is_known_not_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = publication_module._open_exclusive_binary

    class _CloseFailureHandle:
        def __init__(self, wrapped: BinaryIO) -> None:
            self._wrapped = wrapped

        def write(self, data: bytes | memoryview) -> int:
            return self._wrapped.write(data)

        def flush(self) -> None:
            self._wrapped.flush()

        def close(self) -> None:
            self._wrapped.close()
            raise OSError(errno.EIO, "close outcome refused")

    def failing_open(path: Path) -> BinaryIO:
        return cast("BinaryIO", _CloseFailureHandle(original_open(path)))

    monkeypatch.setattr(
        publication_module,
        "_open_exclusive_binary",
        failing_open,
    )
    publication = _allocate(tmp_path)

    with pytest.raises(BundlePublishError) as caught:
        publication.publish(None)

    assert caught.value.phase is BundlePublicationPhase.CLOSE_TEMP
    assert caught.value.replacement_state is ReplacementState.NOT_REPLACED
    assert isinstance(caught.value.__cause__, OSError)
    assert list(publication.path.iterdir()) == []


def test_replace_failure_reports_unknown_and_is_not_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _allocate(tmp_path)
    failure = OSError(errno.EIO, "replace outcome unknown")
    calls = 0

    def fail_replace(*_args: object) -> None:
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(publication_module, "_replace_manifest", fail_replace)
    with pytest.raises(BundlePublishError) as caught:
        publication.publish(None)

    assert caught.value.phase is BundlePublicationPhase.REPLACE_MANIFEST
    assert caught.value.replacement_state is ReplacementState.UNKNOWN
    assert caught.value.__cause__ is failure
    assert calls == 1
    assert not (publication.path / "manifest.json").exists()
    assert list(publication.path.iterdir()) == []

    with pytest.raises(BundlePublishError):
        publication.publish(None)
    assert calls == 1


def test_publication_never_uses_file_or_directory_sync(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)
    writer = publication.open_artifact("stdout.bin")
    writer.write(b"output")
    writer.finalize()
    publication.publish({"complete": True})

    assert "flush_descriptor" not in publication_module.__dict__
    assert "os" not in publication_module.__dict__
    assert "fcntl" not in publication_module.__dict__


def test_partial_writes_stream_all_supplied_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = publication_module._open_exclusive_binary

    class _PartialHandle:
        def __init__(self, wrapped: BinaryIO) -> None:
            self._wrapped = wrapped

        def write(self, data: bytes | memoryview) -> int:
            return self._wrapped.write(data[:2])

        def flush(self) -> None:
            self._wrapped.flush()

        def close(self) -> None:
            self._wrapped.close()

    def partial_open(path: Path) -> BinaryIO:
        return cast("BinaryIO", _PartialHandle(original_open(path)))

    monkeypatch.setattr(
        publication_module,
        "_open_exclusive_binary",
        partial_open,
    )
    publication = _allocate(tmp_path)
    writer = publication.open_artifact("stdout.bin")
    payload = b"complete payload"
    writer.write(payload)
    descriptor = writer.finalize()

    assert (publication.path / "stdout.bin").read_bytes() == payload
    assert descriptor.byte_length == len(payload)


def test_manifest_replacement_is_same_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _allocate(tmp_path)
    original_replace = publication_module._replace_manifest
    replacements: list[tuple[Path, Path]] = []

    def recording_replace(source: Path, target: Path) -> None:
        replacements.append((source, target))
        original_replace(source, target)

    monkeypatch.setattr(
        publication_module,
        "_replace_manifest",
        recording_replace,
    )
    publication.publish(None)

    assert len(replacements) == 1
    source, target = replacements[0]
    assert source.parent == publication.path
    assert target == publication.path / "manifest.json"
    assert source.name.startswith(".dr-store-artifact-bundle-")
