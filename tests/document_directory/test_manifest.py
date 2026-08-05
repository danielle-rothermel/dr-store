"""Manifest publication, deterministic atomic visibility, and read-back."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Self, cast

import pytest

from dr_store import (
    DocumentDirectory,
    ManifestPublishError,
    ManifestReadError,
)
from dr_store.document_directory import directory as directory_module

if TYPE_CHECKING:
    from dr_serialize import Jsonable

MANIFEST_NAME = "record.json"
FIRST: Jsonable = {"state": "started", "sidecars": []}
SECOND: Jsonable = {"state": "finished", "sidecars": ["stdout.bin"]}
WATCHDOG_SECONDS = 60


def _allocate(root: Path) -> DocumentDirectory:
    return DocumentDirectory.allocate(
        root,
        prefix="run",
        manifest_name=MANIFEST_NAME,
    )


def _read(directory: DocumentDirectory) -> Jsonable:
    return DocumentDirectory.read_manifest(
        directory.path,
        manifest_name=MANIFEST_NAME,
    )


def test_publish_writes_canonical_bytes_without_temp_residue(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish({"b": 2, "a": 1})
    assert (directory.path / MANIFEST_NAME).read_bytes() == b'{"a":1,"b":2}'
    assert [path.name for path in directory.path.iterdir()] == [MANIFEST_NAME]


def test_a_reader_sees_old_then_new_across_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    temp_path = directory.path / f"{MANIFEST_NAME}.tmp"
    before_replace = threading.Event()
    allow_replace = threading.Event()
    after_replace = threading.Event()
    allow_completion = threading.Event()
    failures: list[BaseException] = []
    original_replace = Path.replace

    def gated_replace(path: Path, target: Path) -> Path:
        if path == temp_path:
            before_replace.set()
            if not allow_replace.wait(WATCHDOG_SECONDS):
                raise TimeoutError("replace release gate was not opened")
            replaced = original_replace(path, target)
            after_replace.set()
            if not allow_completion.wait(WATCHDOG_SECONDS):
                raise TimeoutError("publication release gate was not opened")
            return replaced
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", gated_replace)

    def publish() -> None:
        try:
            directory.publish(SECOND)
        except BaseException as exc:  # noqa: BLE001 - returned to test thread
            failures.append(exc)

    publishing = threading.Thread(target=publish)
    publishing.start()
    try:
        assert before_replace.wait(WATCHDOG_SECONDS)
        assert temp_path.read_bytes() == (
            b'{"sidecars":["stdout.bin"],"state":"finished"}'
        )
        assert _read(directory) == FIRST
        allow_replace.set()
        assert after_replace.wait(WATCHDOG_SECONDS)
        assert _read(directory) == SECOND
    finally:
        allow_replace.set()
        allow_completion.set()
        publishing.join(timeout=WATCHDOG_SECONDS)

    assert not publishing.is_alive()
    assert failures == []
    assert _read(directory) == SECOND
    assert not temp_path.exists()


class _FailingWriteHandle:
    def __init__(self, wrapped: BinaryIO) -> None:
        self._wrapped = wrapped

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        self._wrapped.close()

    def write(self, _chunk: bytes) -> int:
        raise OSError("write failed")


def test_write_failure_preserves_old_manifest_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    temp_path = directory.path / f"{MANIFEST_NAME}.tmp"
    original_open = Path.open

    def fail_write_open(
        path: Path,
        mode: str,
    ) -> BinaryIO | _FailingWriteHandle:
        wrapped = cast("BinaryIO", original_open(path, mode))
        if path == temp_path:
            return _FailingWriteHandle(wrapped)
        return wrapped

    monkeypatch.setattr(Path, "open", fail_write_open)

    with pytest.raises(ManifestPublishError) as caught:
        directory.publish(SECOND)

    assert isinstance(caught.value.__cause__, OSError)
    assert _read(directory) == FIRST
    assert not temp_path.exists()


def test_descriptor_flush_failure_preserves_old_manifest_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)

    def fail_flush(_descriptor: int) -> None:
        raise OSError("descriptor flush failed")

    monkeypatch.setattr(directory_module, "flush_descriptor", fail_flush)

    with pytest.raises(ManifestPublishError) as caught:
        directory.publish(SECOND)

    assert isinstance(caught.value.__cause__, OSError)
    assert _read(directory) == FIRST
    assert not (directory.path / f"{MANIFEST_NAME}.tmp").exists()


def test_replace_failure_preserves_old_manifest_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    temp_path = directory.path / f"{MANIFEST_NAME}.tmp"
    original_replace = Path.replace

    def fail_replace(path: Path, target: Path) -> Path:
        if path == temp_path:
            raise OSError("replace failed")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(ManifestPublishError) as caught:
        directory.publish(SECOND)

    assert isinstance(caught.value.__cause__, OSError)
    assert _read(directory) == FIRST
    assert not temp_path.exists()


def test_directory_flush_failure_reports_error_after_new_manifest_is_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)

    def fail_flush(_path: Path) -> None:
        raise OSError("directory flush failed")

    monkeypatch.setattr(directory_module, "flush_directory", fail_flush)

    with pytest.raises(ManifestPublishError) as caught:
        directory.publish(SECOND)

    assert isinstance(caught.value.__cause__, OSError)
    assert _read(directory) == SECOND
    assert not (directory.path / f"{MANIFEST_NAME}.tmp").exists()


def test_unremovable_temp_path_preserves_the_typed_publish_error(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path)
    (directory.path / f"{MANIFEST_NAME}.tmp").mkdir()

    with pytest.raises(ManifestPublishError) as caught:
        directory.publish(FIRST)

    assert isinstance(caught.value.__cause__, OSError)


def test_publish_is_last_write_wins(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)
    directory.publish(SECOND)
    assert _read(directory) == SECOND


def test_non_strict_publish_is_typed_and_preserves_previous_manifest(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path)
    directory.publish(FIRST)

    with pytest.raises(ManifestPublishError) as caught:
        directory.publish({"bad": float("inf")})

    assert caught.value.__cause__ is not None
    assert _read(directory) == FIRST


def test_read_manifest_missing_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    with pytest.raises(ManifestReadError) as caught:
        _read(directory)
    assert isinstance(caught.value.__cause__, OSError)


def test_read_manifest_malformed_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(b'{"truncated":')
    with pytest.raises(ManifestReadError) as caught:
        _read(directory)
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


def test_read_manifest_non_strict_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(b'{"value":NaN}')
    with pytest.raises(ManifestReadError) as caught:
        _read(directory)
    assert caught.value.__cause__ is not None


def test_read_manifest_non_canonical_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(b'{"a": 1}')
    with pytest.raises(ManifestReadError):
        _read(directory)


def test_read_manifest_undecodable_bytes_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / MANIFEST_NAME).write_bytes(b"\xff\xfe\x00not utf-8")
    with pytest.raises(ManifestReadError) as caught:
        _read(directory)
    assert caught.value.__cause__ is not None
