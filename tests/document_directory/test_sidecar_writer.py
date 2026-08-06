from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from dr_store import AllocationError, DocumentDirectory, SidecarSummary
from dr_store.document_directory import sidecar as sidecar_module

MANIFEST_NAME = "record.json"
MANIFEST_MAX_BYTES = 1 << 20
SIDECAR_NAME = "stdout.bin"
STREAM = bytes(range(256)) * 8


def _allocate(root: Path) -> DocumentDirectory:
    return DocumentDirectory.allocate(
        root,
        prefix="run",
        manifest_name=MANIFEST_NAME,
        manifest_max_bytes=MANIFEST_MAX_BYTES,
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
    return directory.path / SIDECAR_NAME, writer.finalize()


def _expected_summary(
    *,
    stored: bytes,
    head_length: int,
    tail_length: int,
    produced: int,
) -> SidecarSummary:
    return SidecarSummary(
        head_length=head_length,
        tail_length=tail_length,
        produced=produced,
        dropped=produced - len(stored),
        digest=hashlib.sha256(stored).hexdigest(),
    )


@pytest.mark.parametrize("tail_cap", [None, 10])
def test_unbounded_head_stores_every_byte(
    tmp_path: Path,
    tail_cap: int | None,
) -> None:
    path, summary = _write(
        tmp_path,
        STREAM,
        head_cap=None,
        tail_cap=tail_cap,
    )
    assert path.read_bytes() == STREAM
    assert summary == _expected_summary(
        stored=STREAM,
        head_length=len(STREAM),
        tail_length=0,
        produced=len(STREAM),
    )


def test_head_and_tail_recover_exact_segments(tmp_path: Path) -> None:
    head_cap, tail_cap = 100, 60
    path, summary = _write(
        tmp_path,
        STREAM,
        head_cap=head_cap,
        tail_cap=tail_cap,
    )
    stored = STREAM[:head_cap] + STREAM[-tail_cap:]
    assert path.read_bytes() == stored
    assert summary == _expected_summary(
        stored=stored,
        head_length=head_cap,
        tail_length=tail_cap,
        produced=len(STREAM),
    )


@pytest.mark.parametrize("tail_cap", [None, 0])
def test_finite_head_without_tail_drops_the_remainder(
    tmp_path: Path,
    tail_cap: int | None,
) -> None:
    head_cap = 128
    path, summary = _write(
        tmp_path,
        STREAM,
        head_cap=head_cap,
        tail_cap=tail_cap,
    )
    stored = STREAM[:head_cap]
    assert path.read_bytes() == stored
    assert summary == _expected_summary(
        stored=stored,
        head_length=head_cap,
        tail_length=0,
        produced=len(STREAM),
    )


def test_zero_head_keeps_only_the_tail(tmp_path: Path) -> None:
    tail_cap = 64
    path, summary = _write(
        tmp_path,
        STREAM,
        head_cap=0,
        tail_cap=tail_cap,
    )
    stored = STREAM[-tail_cap:]
    assert path.read_bytes() == stored
    assert summary == _expected_summary(
        stored=stored,
        head_length=0,
        tail_length=tail_cap,
        produced=len(STREAM),
    )


@pytest.mark.parametrize(
    "case",
    [
        (STREAM, 10_000, 10_000, len(STREAM), 0),
        (STREAM, 1024, 1024, 1024, 1024),
        (STREAM[:161], 100, 60, 100, 60),
    ],
    ids=["caps-larger-than-stream", "exact-fill", "one-byte-over"],
)
def test_cap_boundaries_store_exact_segments(
    tmp_path: Path,
    case: tuple[bytes, int, int, int, int],
) -> None:
    payload, head_cap, tail_cap, expected_head, expected_tail = case
    path, summary = _write(
        tmp_path,
        payload,
        head_cap=head_cap,
        tail_cap=tail_cap,
    )
    stored = payload[:expected_head]
    if expected_tail:
        stored += payload[-expected_tail:]
    assert path.read_bytes() == stored
    assert summary == _expected_summary(
        stored=stored,
        head_length=expected_head,
        tail_length=expected_tail,
        produced=len(payload),
    )


def test_empty_sidecar_summarizes_as_empty(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, b"", head_cap=None, tail_cap=None)
    assert path.read_bytes() == b""
    assert summary == _expected_summary(
        stored=b"",
        head_length=0,
        tail_length=0,
        produced=0,
    )


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 97, 4096])
def test_chunking_preserves_exact_bytes_summary_and_digest(
    tmp_path: Path,
    chunk_size: int,
) -> None:
    head_cap, tail_cap = 100, 60
    path, summary = _write(
        tmp_path,
        STREAM,
        head_cap=head_cap,
        tail_cap=tail_cap,
        chunk_size=chunk_size,
    )
    stored = STREAM[:head_cap] + STREAM[-tail_cap:]
    assert path.read_bytes() == stored
    assert summary == _expected_summary(
        stored=stored,
        head_length=head_cap,
        tail_length=tail_cap,
        produced=len(STREAM),
    )


@pytest.mark.parametrize(
    ("head_cap", "tail_cap"),
    [(-5, -5), (-1, 60), (100, -1)],
)
def test_a_negative_cap_is_rejected_before_open(
    tmp_path: Path,
    head_cap: int,
    tail_cap: int,
) -> None:
    directory = _allocate(tmp_path)
    with pytest.raises(AllocationError):
        directory.open_sidecar(
            SIDECAR_NAME,
            head_cap=head_cap,
            tail_cap=tail_cap,
        )
    assert not (directory.path / SIDECAR_NAME).exists()


def test_summary_is_frozen(tmp_path: Path) -> None:
    _, summary = _write(tmp_path, b"x", head_cap=None, tail_cap=None)
    with pytest.raises(AttributeError):
        summary.head_length = 99  # ty: ignore[invalid-assignment]


class _FailOnceHandle:
    def __init__(self, wrapped: BinaryIO) -> None:
        self._wrapped = wrapped
        self._failed = False

    @property
    def closed(self) -> bool:
        return self._wrapped.closed

    def write(self, chunk: bytes) -> int:
        if not self._failed:
            self._failed = True
            raise OSError("write failed")
        return self._wrapped.write(chunk)

    def flush(self) -> None:
        self._wrapped.flush()

    def fileno(self) -> int:
        return self._wrapped.fileno()

    def close(self) -> None:
        self._wrapped.close()


def test_a_failed_write_is_typed_but_does_not_close_the_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    original_open = Path.open
    opened: list[_FailOnceHandle] = []

    def fail_once_open(path: Path, mode: str) -> _FailOnceHandle:
        wrapped = _FailOnceHandle(cast("BinaryIO", original_open(path, mode)))
        opened.append(wrapped)
        return wrapped

    monkeypatch.setattr(Path, "open", fail_once_open)
    writer = directory.open_sidecar(SIDECAR_NAME)

    try:
        with pytest.raises(AllocationError) as caught:
            writer.write(b"not-stored")
        assert isinstance(caught.value.__cause__, OSError)
        assert opened[0].closed is False
    finally:
        opened[0].close()


def test_a_failed_finalize_is_typed_and_closes_the_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    writer = directory.open_sidecar(SIDECAR_NAME)
    writer.write(b"streamed")

    def failing_flush(_descriptor: int) -> None:
        raise OSError("flush refused")

    monkeypatch.setattr(sidecar_module, "flush_descriptor", failing_flush)
    with pytest.raises(AllocationError) as caught:
        writer.finalize()
    assert isinstance(caught.value.__cause__, OSError)

    with pytest.raises(AllocationError) as rejected:
        writer.write(b"after")
    assert isinstance(rejected.value.__cause__, ValueError)
