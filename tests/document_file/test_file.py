from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import pytest

from dr_store.document_file import (
    CanonicalJsonFile,
    DocumentFileError,
    DocumentPublishError,
    DocumentReadError,
    PublicationStage,
)
from dr_store.document_file import file as file_module

if TYPE_CHECKING:
    from pathlib import Path

    from dr_serialize import Jsonable

MAX_BYTES = 1 << 12
WATCHDOG_SECONDS = 60


def _file(directory: Path, *, max_bytes: int = MAX_BYTES) -> CanonicalJsonFile:
    return CanonicalJsonFile(
        directory,
        "document.json",
        max_bytes=max_bytes,
    )


def _nested(depth: int) -> Jsonable:
    value: Jsonable = None
    for _ in range(depth):
        value = [value]
    return value


def test_package_and_class_public_surfaces_are_exact() -> None:
    import dr_store.document_file as package

    assert package.__all__ == [
        "CanonicalJsonFile",
        "DocumentFileError",
        "DocumentPublishError",
        "DocumentReadError",
        "PublicationStage",
    ]
    public = {
        name for name in dir(CanonicalJsonFile) if not name.startswith("_")
    }
    assert public == {"path", "publish", "read"}


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "a\0b"])
def test_constructor_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(DocumentFileError):
        CanonicalJsonFile(tmp_path, name, max_bytes=10)


def test_constructor_rejects_reserved_temp_namespace(tmp_path: Path) -> None:
    with pytest.raises(DocumentFileError):
        CanonicalJsonFile(
            tmp_path,
            ".DR-STORE-DOCUMENT-owned",
            max_bytes=10,
        )


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("max_bytes", -1),
        ("max_bytes", True),
        ("max_bytes", 1.5),
        ("max_depth", -1),
        ("max_depth", False),
        ("max_depth", 1.5),
    ],
)
def test_constructor_rejects_invalid_limits(
    tmp_path: Path,
    argument: str,
    value: object,
) -> None:
    arguments: dict[str, object] = {"max_bytes": 10, "max_depth": 10}
    arguments[argument] = value
    with pytest.raises(DocumentFileError):
        CanonicalJsonFile(tmp_path, "document.json", **arguments)  # type: ignore[arg-type]


def test_constructor_requires_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(DocumentFileError):
        _file(tmp_path / "missing")
    file_path = tmp_path / "not-a-directory"
    file_path.touch()
    with pytest.raises(DocumentFileError):
        _file(file_path)


def test_publish_and_read_exact_canonical_bytes(tmp_path: Path) -> None:
    document_file = _file(tmp_path)
    assert document_file.path == tmp_path / "document.json"

    document_file.publish({"b": 2, "a": 1})

    assert document_file.path.read_bytes() == b'{"a":1,"b":2}'
    assert document_file.read() == {"a": 1, "b": 2}
    assert [path.name for path in tmp_path.iterdir()] == ["document.json"]


def test_publish_replaces_existing_document(tmp_path: Path) -> None:
    document_file = _file(tmp_path)
    document_file.publish({"version": 1})
    document_file.publish({"version": 2})
    assert document_file.read() == {"version": 2}


def test_publish_enforces_byte_bound_before_filesystem_write(
    tmp_path: Path,
) -> None:
    document_file = _file(tmp_path, max_bytes=3)
    with pytest.raises(DocumentPublishError) as caught:
        document_file.publish(None)
    assert caught.value.stage is PublicationStage.ENCODE
    assert caught.value.replacement_completed is False
    assert not document_file.path.exists()


def test_publish_enforces_lower_depth_bound_before_filesystem_write(
    tmp_path: Path,
) -> None:
    document_file = CanonicalJsonFile(
        tmp_path,
        "document.json",
        max_bytes=MAX_BYTES,
        max_depth=2,
    )
    with pytest.raises(DocumentPublishError) as caught:
        document_file.publish(_nested(3))
    assert caught.value.stage is PublicationStage.ENCODE
    assert caught.value.replacement_completed is False
    assert not document_file.path.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"\xef\xbb\xbfnull",
        b'{"a":1,"a":2}',
        b'{"value":NaN}',
        b"null true",
        b'{"a": 1}',
    ],
    ids=["utf8", "bom", "duplicate", "nonfinite", "trailing", "canonical"],
)
def test_read_rejects_non_strict_or_noncanonical_bytes(
    tmp_path: Path,
    raw: bytes,
) -> None:
    document_file = _file(tmp_path)
    document_file.path.write_bytes(raw)
    with pytest.raises(DocumentReadError) as caught:
        document_file.read()
    assert caught.value.path == document_file.path
    assert caught.value.__cause__ is not None


def test_read_rejects_oversized_and_excessively_nested_documents(
    tmp_path: Path,
) -> None:
    oversized = _file(tmp_path, max_bytes=3)
    oversized.path.write_bytes(b"null")
    with pytest.raises(DocumentReadError):
        oversized.read()

    nested = CanonicalJsonFile(
        tmp_path,
        "nested.json",
        max_bytes=MAX_BYTES,
        max_depth=1,
    )
    nested.path.write_bytes(b"[[null]]")
    with pytest.raises(DocumentReadError):
        nested.read()


def test_read_missing_is_typed(tmp_path: Path) -> None:
    document_file = _file(tmp_path)
    with pytest.raises(DocumentReadError) as caught:
        document_file.read()
    assert isinstance(caught.value.__cause__, OSError)


def test_read_rejects_final_symlink_and_directory(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b"null")
    link_file = _file(tmp_path)
    link_file.path.symlink_to(target)
    with pytest.raises(DocumentReadError):
        link_file.read()

    directory_file = CanonicalJsonFile(
        tmp_path,
        "child",
        max_bytes=MAX_BYTES,
    )
    directory_file.path.mkdir()
    with pytest.raises(DocumentReadError):
        directory_file.read()


def test_read_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    document_file = _file(tmp_path)
    os.mkfifo(document_file.path)
    with pytest.raises(DocumentReadError):
        document_file.read()


def test_partial_writes_are_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = os.write
    offered_lengths: list[int] = []

    def partial_write(descriptor: int, data: bytes) -> int:
        offered_lengths.append(len(data))
        return original_write(descriptor, data[:2])

    monkeypatch.setattr(file_module.os, "write", partial_write)
    document_file = _file(tmp_path)
    document_file.publish({"value": "complete"})
    assert document_file.read() == {"value": "complete"}
    assert len(offered_lengths) > 1
    assert offered_lengths == sorted(offered_lengths, reverse=True)


def test_zero_progress_write_is_typed_and_preserves_prior_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)
    document_file.publish({"version": 1})
    monkeypatch.setattr(file_module.os, "write", lambda *_args: 0)

    with pytest.raises(DocumentPublishError) as caught:
        document_file.publish({"version": 2})

    assert caught.value.stage is PublicationStage.WRITE_TEMP
    assert caught.value.replacement_completed is False
    assert document_file.read() == {"version": 1}
    assert [path.name for path in tmp_path.iterdir()] == ["document.json"]


def test_two_publishers_use_distinct_temps_and_last_replacement_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)
    document_file.publish({"writer": "initial"})
    original_replace = file_module._replace
    arrived = threading.Barrier(3)
    allow = {"first": threading.Event(), "second": threading.Event()}
    replaced = {"first": threading.Event(), "second": threading.Event()}
    temporary_names: dict[str, str] = {}
    failures: list[BaseException] = []

    def gated_replace(
        source: str,
        target: str,
        *,
        directory_descriptor: int,
    ) -> None:
        writer = threading.current_thread().name
        temporary_names[writer] = source
        arrived.wait(timeout=WATCHDOG_SECONDS)
        if not allow[writer].wait(WATCHDOG_SECONDS):
            raise TimeoutError("replacement release gate was not opened")
        original_replace(
            source,
            target,
            directory_descriptor=directory_descriptor,
        )
        replaced[writer].set()

    monkeypatch.setattr(file_module, "_replace", gated_replace)

    def publish(writer: str) -> None:
        try:
            document_file.publish({"writer": writer})
        except BaseException as exc:  # noqa: BLE001 - returned to test owner
            failures.append(exc)

    threads = [
        threading.Thread(target=publish, args=(writer,), name=writer)
        for writer in ("first", "second")
    ]
    for thread in threads:
        thread.start()
    try:
        arrived.wait(timeout=WATCHDOG_SECONDS)
        assert len(set(temporary_names.values())) == 2
        allow["first"].set()
        assert replaced["first"].wait(WATCHDOG_SECONDS)
        allow["second"].set()
        assert replaced["second"].wait(WATCHDOG_SECONDS)
    finally:
        for event in allow.values():
            event.set()
        for thread in threads:
            thread.join(timeout=WATCHDOG_SECONDS)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert document_file.read() == {"writer": "second"}
    assert [path.name for path in tmp_path.iterdir()] == ["document.json"]


def test_read_stays_on_opened_inode_across_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_file = _file(tmp_path)
    old: Jsonable = {"version": "old"}
    new: Jsonable = {"version": "new"}
    document_file.publish(old)
    original_open = file_module.os.open
    original_read = file_module.os.read
    child_descriptor: list[int] = []
    child_opened = threading.Event()
    allow_read = threading.Event()
    result: list[Jsonable] = []
    failures: list[BaseException] = []

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "document.json" and dir_fd is not None:
            child_descriptor.append(descriptor)
            child_opened.set()
        return descriptor

    def gated_read(descriptor: int, size: int) -> bytes:
        if (
            child_descriptor
            and descriptor == child_descriptor[0]
            and not allow_read.wait(WATCHDOG_SECONDS)
        ):
            raise TimeoutError("read release gate was not opened")
        return original_read(descriptor, size)

    monkeypatch.setattr(file_module.os, "open", recording_open)
    monkeypatch.setattr(file_module.os, "read", gated_read)

    def read_old() -> None:
        try:
            result.append(document_file.read())
        except BaseException as exc:  # noqa: BLE001 - returned to test owner
            failures.append(exc)

    reader = threading.Thread(target=read_old)
    reader.start()
    try:
        assert child_opened.wait(WATCHDOG_SECONDS)
        document_file.publish(new)
    finally:
        allow_read.set()
        reader.join(timeout=WATCHDOG_SECONDS)

    assert not reader.is_alive()
    assert failures == []
    assert result == [old]
    assert document_file.read() == new
