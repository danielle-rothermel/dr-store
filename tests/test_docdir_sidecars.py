"""Sidecar truncation mechanics, accounting, and verified read-back.

The writer owns truncation entirely: ``head_cap`` bytes fill first, a ring
buffer keeps the last ``tail_cap`` bytes of the remainder, and the stored
file is the head segment followed by the tail segment. The summary reports
the stored segment lengths alongside ``produced`` and ``dropped``, and the
Sidecar Digest covers exactly the stored bytes.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from dr_store import (
    AllocationError,
    DocumentDirectory,
    SidecarSummary,
    SidecarVerificationError,
)

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST_NAME = "record.json"
SIDECAR_NAME = "stdout.bin"
STREAM = bytes(range(256)) * 8  # 2048 deterministic bytes


def _allocate(root: Path) -> DocumentDirectory:
    return DocumentDirectory.allocate(
        root,
        prefix="run",
        manifest_name=MANIFEST_NAME,
    )


def _write(
    root: Path,
    payload: bytes,
    *,
    head_cap: int | None,
    tail_cap: int | None,
    chunk_size: int = 97,
) -> tuple[Path, SidecarSummary]:
    directory = _allocate(root)
    writer = directory.open_sidecar(
        SIDECAR_NAME,
        head_cap=head_cap,
        tail_cap=tail_cap,
    )
    for start in range(0, len(payload), chunk_size):
        writer.write(payload[start : start + chunk_size])
    return writer.path, writer.finalize()


def test_unbounded_sidecar_stores_every_byte(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=None, tail_cap=None)
    assert path.read_bytes() == STREAM
    assert summary == SidecarSummary(
        head_length=len(STREAM),
        tail_length=0,
        produced=len(STREAM),
        dropped=0,
        digest=hashlib.sha256(STREAM).hexdigest(),
    )


def test_head_and_tail_recover_exact_segments(tmp_path: Path) -> None:
    head_cap, tail_cap = 100, 60
    path, summary = _write(
        tmp_path,
        STREAM,
        head_cap=head_cap,
        tail_cap=tail_cap,
    )
    expected = STREAM[:head_cap] + STREAM[-tail_cap:]
    assert path.read_bytes() == expected
    assert summary.head_length == head_cap
    assert summary.tail_length == tail_cap
    assert summary.produced == len(STREAM)
    assert summary.dropped == len(STREAM) - head_cap - tail_cap
    assert summary.digest == hashlib.sha256(expected).hexdigest()


def test_head_only_drops_the_whole_remainder(tmp_path: Path) -> None:
    head_cap = 128
    path, summary = _write(tmp_path, STREAM, head_cap=head_cap, tail_cap=0)
    assert path.read_bytes() == STREAM[:head_cap]
    assert summary.head_length == head_cap
    assert summary.tail_length == 0
    assert summary.dropped == len(STREAM) - head_cap


def test_zero_head_keeps_only_the_tail(tmp_path: Path) -> None:
    tail_cap = 64
    path, summary = _write(tmp_path, STREAM, head_cap=0, tail_cap=tail_cap)
    assert path.read_bytes() == STREAM[-tail_cap:]
    assert summary.head_length == 0
    assert summary.tail_length == tail_cap
    assert summary.dropped == len(STREAM) - tail_cap


def test_caps_larger_than_the_stream_drop_nothing(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=10_000, tail_cap=10_000)
    assert path.read_bytes() == STREAM
    assert summary.head_length == len(STREAM)
    assert summary.tail_length == 0
    assert summary.dropped == 0


def test_stream_exactly_filling_the_caps_drops_nothing(
    tmp_path: Path,
) -> None:
    head_cap, tail_cap = 1024, 1024
    path, summary = _write(
        tmp_path,
        STREAM,
        head_cap=head_cap,
        tail_cap=tail_cap,
    )
    assert path.read_bytes() == STREAM
    assert summary.head_length == head_cap
    assert summary.tail_length == tail_cap
    assert summary.dropped == 0


def test_one_byte_over_the_caps_drops_exactly_one(tmp_path: Path) -> None:
    payload = STREAM[:161]
    path, summary = _write(tmp_path, payload, head_cap=100, tail_cap=60)
    assert path.read_bytes() == payload[:100] + payload[-60:]
    assert summary.dropped == 1
    assert summary.produced == 161


def test_empty_sidecar_summarizes_as_empty(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, b"", head_cap=None, tail_cap=None)
    assert path.read_bytes() == b""
    assert summary.produced == 0
    assert summary.dropped == 0
    assert summary.digest == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 97, 4096])
def test_accounting_holds_for_every_chunking(
    tmp_path: Path,
    chunk_size: int,
) -> None:
    _, summary = _write(
        tmp_path,
        STREAM,
        head_cap=100,
        tail_cap=60,
        chunk_size=chunk_size,
    )
    total = summary.head_length + summary.tail_length + summary.dropped
    assert total == summary.produced == len(STREAM)


def test_finalize_is_idempotent(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    writer = directory.open_sidecar(SIDECAR_NAME, head_cap=4, tail_cap=4)
    writer.write(STREAM)
    first = writer.finalize()
    assert writer.finalize() == first
    assert writer.path.read_bytes() == STREAM[:4] + STREAM[-4:]


def test_write_after_finalize_is_rejected(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    writer = directory.open_sidecar(SIDECAR_NAME)
    writer.write(b"before")
    writer.finalize()
    with pytest.raises(ValueError, match="already finalized"):
        writer.write(b"after")


def test_context_manager_finalizes(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    with directory.open_sidecar(SIDECAR_NAME) as writer:
        writer.write(b"streamed")
    summary = writer.finalize()
    assert summary.head_length == len(b"streamed")
    assert writer.path.read_bytes() == b"streamed"


def test_negative_caps_are_rejected(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    with pytest.raises(ValueError, match="head_cap"):
        directory.open_sidecar(SIDECAR_NAME, head_cap=-1)
    with pytest.raises(ValueError, match="tail_cap"):
        directory.open_sidecar(SIDECAR_NAME, tail_cap=-1)


def test_several_sidecars_live_side_by_side(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    summaries = {}
    for name in ("stdout.bin", "stderr.bin"):
        writer = directory.open_sidecar(name)
        writer.write(name.encode("utf-8"))
        summaries[name] = writer.finalize()
    for name, summary in summaries.items():
        DocumentDirectory.verify_sidecar(
            directory.path / name,
            expected_digest=summary.digest,
            expected_head_length=summary.head_length,
            expected_tail_length=summary.tail_length,
        )


def test_verify_sidecar_accepts_matching_bytes(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=100, tail_cap=60)
    DocumentDirectory.verify_sidecar(
        path,
        expected_digest=summary.digest,
        expected_head_length=summary.head_length,
        expected_tail_length=summary.tail_length,
    )


def test_verify_sidecar_rejects_mutated_bytes(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=100, tail_cap=60)
    stored = bytearray(path.read_bytes())
    stored[50] ^= 0xFF
    path.write_bytes(bytes(stored))
    with pytest.raises(SidecarVerificationError, match="digest mismatch"):
        DocumentDirectory.verify_sidecar(
            path,
            expected_digest=summary.digest,
            expected_head_length=summary.head_length,
            expected_tail_length=summary.tail_length,
        )


def test_verify_sidecar_rejects_mismatched_lengths(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=100, tail_cap=60)
    with pytest.raises(SidecarVerificationError, match="length mismatch"):
        DocumentDirectory.verify_sidecar(
            path,
            expected_digest=summary.digest,
            expected_head_length=summary.head_length + 1,
            expected_tail_length=summary.tail_length,
        )


def test_verify_sidecar_rejects_truncated_file(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=100, tail_cap=60)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(SidecarVerificationError, match="length mismatch"):
        DocumentDirectory.verify_sidecar(
            path,
            expected_digest=summary.digest,
            expected_head_length=summary.head_length,
            expected_tail_length=summary.tail_length,
        )


def test_verify_sidecar_missing_file_is_typed(tmp_path: Path) -> None:
    with pytest.raises(SidecarVerificationError) as caught:
        DocumentDirectory.verify_sidecar(
            tmp_path / "absent.bin",
            expected_digest="0" * 64,
            expected_head_length=0,
            expected_tail_length=0,
        )
    assert isinstance(caught.value.__cause__, OSError)


def test_summary_is_frozen(tmp_path: Path) -> None:
    _, summary = _write(tmp_path, b"x", head_cap=None, tail_cap=None)
    with pytest.raises(AttributeError):
        summary.head_length = 99  # ty: ignore[invalid-assignment]


def test_sidecar_names_are_validated(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    for bad in ("..", ".", "", "nested/name", "esc\\ape", "nul\x00byte"):
        with pytest.raises(AllocationError):
            directory.open_sidecar(bad)
