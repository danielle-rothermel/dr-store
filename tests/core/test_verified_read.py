from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from dr_store import VerifiedRegularChildReadError, read_verified_regular_child
from dr_store.core import verified_read as verified_read_module

if TYPE_CHECKING:
    from pathlib import Path


def test_read_verified_regular_child_returns_matching_bytes(
    tmp_path: Path,
) -> None:
    payload = b"verified-bytes"
    child_name = "artifact.bin"
    (tmp_path / child_name).write_bytes(payload)

    assert (
        read_verified_regular_child(
            tmp_path,
            child_name,
            max_bytes=len(payload),
            expected_byte_length=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        == payload
    )


def test_read_verified_regular_child_rejects_hash_mismatch(
    tmp_path: Path,
) -> None:
    payload = b"stored"
    child_name = "artifact.bin"
    (tmp_path / child_name).write_bytes(payload)

    with pytest.raises(VerifiedRegularChildReadError, match="hash mismatch"):
        read_verified_regular_child(
            tmp_path,
            child_name,
            max_bytes=len(payload),
            expected_byte_length=len(payload),
            expected_sha256="0" * 64,
        )


def test_read_verified_regular_child_rejects_length_mismatch(
    tmp_path: Path,
) -> None:
    payload = b"stored"
    child_name = "artifact.bin"
    (tmp_path / child_name).write_bytes(payload)

    with pytest.raises(VerifiedRegularChildReadError, match="length mismatch"):
        read_verified_regular_child(
            tmp_path,
            child_name,
            max_bytes=len(payload) + 4,
            expected_byte_length=len(payload) + 1,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_read_verified_regular_child_rejects_bound_exceeded(
    tmp_path: Path,
) -> None:
    payload = b"0123456789"
    child_name = "artifact.bin"
    (tmp_path / child_name).write_bytes(payload)

    with pytest.raises(VerifiedRegularChildReadError, match="exceeds the"):
        read_verified_regular_child(
            tmp_path,
            child_name,
            max_bytes=4,
            expected_byte_length=4,
            expected_sha256=hashlib.sha256(payload[:4]).hexdigest(),
        )


def test_read_verified_regular_child_rejects_non_regular_child(
    tmp_path: Path,
) -> None:
    child_name = "child-dir"
    (tmp_path / child_name).mkdir()

    with pytest.raises(
        VerifiedRegularChildReadError,
        match="is not a regular file",
    ):
        read_verified_regular_child(
            tmp_path,
            child_name,
            max_bytes=0,
            expected_byte_length=0,
            expected_sha256=hashlib.sha256(b"").hexdigest(),
        )


def test_read_verified_regular_child_fails_closed_without_no_follow_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"stored"
    child_name = "artifact.bin"
    (tmp_path / child_name).write_bytes(payload)
    monkeypatch.delattr(verified_read_module.os, "O_NOFOLLOW")

    with pytest.raises(VerifiedRegularChildReadError) as caught:
        read_verified_regular_child(
            tmp_path,
            child_name,
            max_bytes=len(payload),
            expected_byte_length=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
    assert caught.value.__cause__ is None


def test_read_verified_regular_child_rejects_unsafe_name(
    tmp_path: Path,
) -> None:
    with pytest.raises(VerifiedRegularChildReadError):
        read_verified_regular_child(
            tmp_path,
            "../outside.bin",
            max_bytes=0,
            expected_byte_length=0,
            expected_sha256=hashlib.sha256(b"").hexdigest(),
        )


def test_read_verified_regular_child_detects_overflow_with_one_extra_byte(
    tmp_path: Path,
) -> None:
    payload = b"0123456789"
    child_name = "artifact.bin"
    (tmp_path / child_name).write_bytes(payload)

    with pytest.raises(VerifiedRegularChildReadError, match="exceeds the"):
        read_verified_regular_child(
            tmp_path,
            child_name,
            max_bytes=5,
            expected_byte_length=5,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
