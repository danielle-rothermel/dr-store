from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from dr_store import read_verified_regular_child
from dr_store.core import descriptor_io as descriptor_io_module
from dr_store.core import filesystem as filesystem_module
from dr_store.core.errors import (
    AllocationError,
    RegularChildFailureReason,
    VerifiedRegularChildReadError,
)

if TYPE_CHECKING:
    from pathlib import Path


class _RegularFileError(Exception):
    pass


def test_read_bounded_child_descriptor_returns_within_limit(
    tmp_path: Path,
) -> None:
    payload = b"01234"
    child_path = tmp_path / "child.bin"
    child_path.write_bytes(payload)
    child_descriptor = os.open(child_path, os.O_RDONLY)
    try:
        assert (
            descriptor_io_module.read_bounded_child_descriptor(
                child_descriptor,
                max_bytes=len(payload),
            )
            == payload
        )
    finally:
        os.close(child_descriptor)


def test_read_bounded_child_descriptor_allows_overshoot_probe(
    tmp_path: Path,
) -> None:
    payload = b"0123456789"
    child_path = tmp_path / "child.bin"
    child_path.write_bytes(payload)
    child_descriptor = os.open(child_path, os.O_RDONLY)
    try:
        raw = descriptor_io_module.read_bounded_child_descriptor(
            child_descriptor,
            max_bytes=4,
        )
        assert len(raw) == 5
    finally:
        os.close(child_descriptor)


def test_require_regular_file_rejects_directory(tmp_path: Path) -> None:
    directory_path = tmp_path / "child-dir"
    directory_path.mkdir()
    directory_descriptor = os.open(directory_path, os.O_RDONLY)
    try:
        metadata = os.fstat(directory_descriptor)
        with pytest.raises(_RegularFileError):
            filesystem_module._require_regular_file(
                metadata,
                error=_RegularFileError,
                message="child is not a regular file",
            )
    finally:
        os.close(directory_descriptor)


def test_pinned_read_support_detail_preserves_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(filesystem_module.os, "O_NOFOLLOW")

    detail = filesystem_module._pinned_read_support_detail()
    assert detail == "O_NOFOLLOW"


def test_pinned_read_support_detail_is_none_when_supported() -> None:
    assert filesystem_module._pinned_read_support_detail() is None


def test_validate_safe_name_preserves_caller_error() -> None:
    with pytest.raises(AllocationError):
        filesystem_module.validate_safe_name("..", role="role")


def test_verified_read_rejects_overshoot_via_reason(tmp_path: Path) -> None:
    payload = b"0123456789"
    child_name = "artifact.bin"
    (tmp_path / child_name).write_bytes(payload)

    with pytest.raises(VerifiedRegularChildReadError) as caught:
        read_verified_regular_child(
            tmp_path,
            child_name,
            max_bytes=4,
            expected_byte_length=4,
            expected_sha256="0" * 64,
        )
    assert caught.value.reason is RegularChildFailureReason.BOUNDS_EXCEEDED
