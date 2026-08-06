from __future__ import annotations

import errno
from typing import TYPE_CHECKING

import pytest

from dr_store.document_file import (
    CanonicalJsonFile,
    DocumentPublishError,
    PublicationStage,
    ReplacementState,
)
from dr_store.document_file import file as file_module

if TYPE_CHECKING:
    import os
    from collections.abc import Callable
    from pathlib import Path

    from dr_serialize import Jsonable

MAX_BYTES = 1 << 12
FIRST: Jsonable = {"version": 1}
SECOND: Jsonable = {"version": 2}


def _file(directory: Path) -> CanonicalJsonFile:
    return CanonicalJsonFile(
        directory,
        "document.json",
        max_bytes=MAX_BYTES,
    )


def _assert_failure(
    document_file: CanonicalJsonFile,
    *,
    stage: PublicationStage,
    replacement_state: ReplacementState,
) -> DocumentPublishError:
    with pytest.raises(DocumentPublishError) as caught:
        document_file.publish(SECOND)
    assert caught.value.path == document_file.path
    assert caught.value.stage is stage
    assert caught.value.replacement_state is replacement_state
    assert isinstance(caught.value.__cause__, OSError)
    return caught.value


def test_create_temp_failure_preserves_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)
    document_file.publish(FIRST)
    monkeypatch.setattr(file_module, "_OPEN_SUPPORTS_DIR_FD", False)

    _assert_failure(
        document_file,
        stage=PublicationStage.CREATE_TEMP,
        replacement_state=ReplacementState.NOT_REPLACED,
    )
    assert document_file.path.read_bytes() == b'{"version":1}'


@pytest.mark.parametrize(
    ("seam", "stage", "replacement_state"),
    [
        (
            "write",
            PublicationStage.WRITE_TEMP,
            ReplacementState.NOT_REPLACED,
        ),
        (
            "flush",
            PublicationStage.FLUSH_TEMP,
            ReplacementState.NOT_REPLACED,
        ),
        (
            "replace",
            PublicationStage.REPLACE_TARGET,
            ReplacementState.UNKNOWN,
        ),
    ],
)
def test_pre_replacement_failure_preserves_existing_target_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
    stage: PublicationStage,
    replacement_state: ReplacementState,
) -> None:
    document_file = _file(tmp_path)
    document_file.publish(FIRST)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, f"{seam} failed")

    if seam == "write":
        monkeypatch.setattr(file_module, "_write_all", fail)
    elif seam == "flush":
        monkeypatch.setattr(file_module, "flush_descriptor", fail)
    else:
        monkeypatch.setattr(file_module, "_replace", fail)

    _assert_failure(
        document_file,
        stage=stage,
        replacement_state=replacement_state,
    )
    assert document_file.path.read_bytes() == b'{"version":1}'
    assert [path.name for path in tmp_path.iterdir()] == ["document.json"]


def test_directory_flush_failure_reports_completed_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)
    document_file.publish(FIRST)
    calls = 0
    original_flush = file_module.flush_descriptor

    def fail_second_flush(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "directory flush failed")
        original_flush(descriptor)

    monkeypatch.setattr(file_module, "flush_descriptor", fail_second_flush)
    _assert_failure(
        document_file,
        stage=PublicationStage.FLUSH_DIRECTORY,
        replacement_state=ReplacementState.REPLACED,
    )
    assert document_file.path.read_bytes() == b'{"version":2}'


def _fail_selected_close(
    monkeypatch: pytest.MonkeyPatch,
    selector: Callable[[int], bool],
) -> None:
    original_close = file_module.os.close

    def close_then_fail(descriptor: int) -> None:
        original_close(descriptor)
        if selector(descriptor):
            raise OSError(errno.EIO, "close failed")

    monkeypatch.setattr(file_module.os, "close", close_then_fail)


def test_temp_close_failure_is_flush_temp_and_preserves_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)
    document_file.publish(FIRST)
    original_open = file_module.os.open
    temp_descriptors: set[int] = set()

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None:
            temp_descriptors.add(descriptor)
        return descriptor

    monkeypatch.setattr(file_module.os, "open", recording_open)
    _fail_selected_close(monkeypatch, temp_descriptors.__contains__)

    _assert_failure(
        document_file,
        stage=PublicationStage.FLUSH_TEMP,
        replacement_state=ReplacementState.NOT_REPLACED,
    )
    assert document_file.path.read_bytes() == b'{"version":1}'


def test_directory_close_failure_reports_completed_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)
    document_file.publish(FIRST)
    original_open = file_module.os.open
    directory_descriptors: set[int] = set()

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is None:
            directory_descriptors.add(descriptor)
        return descriptor

    monkeypatch.setattr(file_module.os, "open", recording_open)
    _fail_selected_close(monkeypatch, directory_descriptors.__contains__)

    _assert_failure(
        document_file,
        stage=PublicationStage.FLUSH_DIRECTORY,
        replacement_state=ReplacementState.REPLACED,
    )
    assert document_file.path.read_bytes() == b'{"version":2}'


def test_cleanup_failure_does_not_mask_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)
    primary = OSError(errno.EIO, "write failed")

    def fail_write(*_args: object) -> None:
        raise primary

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EACCES, "cleanup failed")

    monkeypatch.setattr(file_module, "_write_all", fail_write)
    monkeypatch.setattr(file_module.os, "unlink", fail_cleanup)

    caught = _assert_failure(
        document_file,
        stage=PublicationStage.WRITE_TEMP,
        replacement_state=ReplacementState.NOT_REPLACED,
    )
    assert caught.__cause__ is primary
    assert not document_file.path.exists()


def test_replace_failure_reports_unknown_for_absent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "replace failed")

    monkeypatch.setattr(file_module, "_replace", fail_replace)
    _assert_failure(
        document_file,
        stage=PublicationStage.REPLACE_TARGET,
        replacement_state=ReplacementState.UNKNOWN,
    )
    assert not document_file.path.exists()


def test_replace_failure_reports_unknown_when_target_was_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)
    document_file.publish(FIRST)
    original_replace = file_module._replace

    def replace_then_fail(
        source: str,
        target: str,
        *,
        directory_descriptor: int,
    ) -> None:
        original_replace(
            source,
            target,
            directory_descriptor=directory_descriptor,
        )
        raise OSError(errno.EIO, "replace outcome could not be confirmed")

    monkeypatch.setattr(file_module, "_replace", replace_then_fail)
    _assert_failure(
        document_file,
        stage=PublicationStage.REPLACE_TARGET,
        replacement_state=ReplacementState.UNKNOWN,
    )
    assert document_file.path.read_bytes() == b'{"version":2}'
