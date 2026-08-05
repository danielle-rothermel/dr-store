"""Document Directory Sidecar retention, accounting, and read-back.

The writer owns truncation entirely: ``head_cap`` bytes fill first, a ring
buffer keeps the last ``tail_cap`` bytes of the remainder, and the stored
file is the head segment followed by the tail segment. The summary reports
the stored segment lengths alongside ``produced`` and ``dropped``, and the
Sidecar Digest covers exactly the stored bytes.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from dr_store import (
    AllocationError,
    DocumentDirectory,
    SidecarSummary,
    SidecarVerificationError,
)
from dr_store.document_directory import sidecar as sidecar_module

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST_NAME = "record.json"
SIDECAR_NAME = "stdout.bin"
STREAM = bytes(range(256)) * 8  # 2048 deterministic bytes
WATCHDOG_SECONDS = 60


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
    return directory.path / SIDECAR_NAME, writer.finalize()


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


@pytest.mark.parametrize(
    ("head_cap", "tail_cap"),
    [(-5, -5), (-1, 60), (100, -1)],
)
def test_a_negative_cap_is_rejected(
    tmp_path: Path,
    head_cap: int,
    tail_cap: int,
) -> None:
    # A negative cap has no truncation meaning: unrejected, it would evict
    # more bytes than the stream offered and report dropped > produced.
    directory = _allocate(tmp_path)
    with pytest.raises(AllocationError):
        directory.open_sidecar(
            SIDECAR_NAME,
            head_cap=head_cap,
            tail_cap=tail_cap,
        )
    # Rejection precedes the open, so no Sidecar file was created.
    assert not (directory.path / SIDECAR_NAME).exists()


def test_an_unset_tail_cap_stores_only_the_head(tmp_path: Path) -> None:
    # The tail buffer is bounded by tail_cap, never by the stream: with no
    # tail cap under a finite head cap the remainder is dropped, not held.
    head_cap = 128
    path, summary = _write(tmp_path, STREAM, head_cap=head_cap, tail_cap=None)
    assert path.read_bytes() == STREAM[:head_cap]
    assert summary.head_length == head_cap
    assert summary.tail_length == 0
    assert summary.dropped == len(STREAM) - head_cap


def test_an_unset_head_cap_streams_past_any_tail_cap(tmp_path: Path) -> None:
    # An unbounded head takes every byte, so nothing reaches the tail.
    path, summary = _write(tmp_path, STREAM, head_cap=None, tail_cap=10)
    assert path.read_bytes() == STREAM
    assert summary.head_length == len(STREAM)
    assert summary.tail_length == 0
    assert summary.dropped == 0


def test_several_sidecars_live_side_by_side(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    summaries = {}
    for name in ("stdout.bin", "stderr.bin"):
        writer = directory.open_sidecar(name)
        writer.write(name.encode("utf-8"))
        summaries[name] = writer.finalize()
    for name, summary in summaries.items():
        directory.verify_sidecar(
            name,
            expected_digest=summary.digest,
            expected_head_length=summary.head_length,
            expected_tail_length=summary.tail_length,
        )


def test_verify_sidecar_accepts_matching_bytes(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=100, tail_cap=60)
    directory = DocumentDirectory(path.parent, MANIFEST_NAME)
    directory.verify_sidecar(
        path.name,
        expected_digest=summary.digest,
        expected_head_length=summary.head_length,
        expected_tail_length=summary.tail_length,
    )


def test_verify_sidecar_rejects_mutated_bytes(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=100, tail_cap=60)
    stored = bytearray(path.read_bytes())
    stored[50] ^= 0xFF
    path.write_bytes(bytes(stored))
    directory = DocumentDirectory(path.parent, MANIFEST_NAME)
    with pytest.raises(SidecarVerificationError, match="digest mismatch"):
        directory.verify_sidecar(
            path.name,
            expected_digest=summary.digest,
            expected_head_length=summary.head_length,
            expected_tail_length=summary.tail_length,
        )


def test_verify_sidecar_rejects_mismatched_lengths(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=100, tail_cap=60)
    directory = DocumentDirectory(path.parent, MANIFEST_NAME)
    with pytest.raises(SidecarVerificationError, match="length mismatch"):
        directory.verify_sidecar(
            path.name,
            expected_digest=summary.digest,
            expected_head_length=summary.head_length + 1,
            expected_tail_length=summary.tail_length,
        )


def test_verify_sidecar_rejects_truncated_file(tmp_path: Path) -> None:
    path, summary = _write(tmp_path, STREAM, head_cap=100, tail_cap=60)
    path.write_bytes(path.read_bytes()[:-1])
    directory = DocumentDirectory(path.parent, MANIFEST_NAME)
    with pytest.raises(SidecarVerificationError, match="length mismatch"):
        directory.verify_sidecar(
            path.name,
            expected_digest=summary.digest,
            expected_head_length=summary.head_length,
            expected_tail_length=summary.tail_length,
        )


def test_verify_sidecar_missing_file_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    with pytest.raises(SidecarVerificationError) as caught:
        directory.verify_sidecar(
            "absent.bin",
            expected_digest="0" * 64,
            expected_head_length=0,
            expected_tail_length=0,
        )
    assert isinstance(caught.value.__cause__, OSError)


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "/absolute.bin",
        "nested/name.bin",
        "nested\\name.bin",
        "nul\x00name.bin",
        MANIFEST_NAME,
        f"{MANIFEST_NAME}.tmp",
    ],
)
def test_verify_sidecar_rejects_unsafe_and_reserved_names_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    directory = _allocate(tmp_path)

    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        pytest.fail("unsafe Sidecar name reached the filesystem open")

    monkeypatch.setattr(sidecar_module.os, "open", unexpected_open)
    with pytest.raises(SidecarVerificationError):
        directory.verify_sidecar(
            name,
            expected_digest=hashlib.sha256(b"").hexdigest(),
            expected_head_length=0,
            expected_tail_length=0,
        )


@pytest.mark.parametrize("target_location", ["outside", "inside"])
def test_verify_sidecar_rejects_final_component_symlinks(
    tmp_path: Path,
    target_location: str,
) -> None:
    directory = _allocate(tmp_path)
    payload = b"matching bytes must not make a symlink acceptable"
    if target_location == "outside":
        target = tmp_path / "outside.bin"
        link_target = target
    else:
        target = directory.path / "target.bin"
        link_target = target.name
    target.write_bytes(payload)
    (directory.path / SIDECAR_NAME).symlink_to(link_target)

    with pytest.raises(SidecarVerificationError) as caught:
        directory.verify_sidecar(
            SIDECAR_NAME,
            expected_digest=hashlib.sha256(payload).hexdigest(),
            expected_head_length=len(payload),
            expected_tail_length=0,
        )
    assert isinstance(caught.value.__cause__, OSError)


def test_verify_sidecar_rejects_a_symlinked_directory_authority(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path)
    payload = b"matching attacker bytes must not retarget the directory"
    attacker_directory = tmp_path / "attacker-directory"
    attacker_directory.mkdir()
    (attacker_directory / SIDECAR_NAME).write_bytes(payload)
    directory.path.rmdir()
    directory.path.symlink_to(attacker_directory, target_is_directory=True)

    with pytest.raises(SidecarVerificationError) as caught:
        directory.verify_sidecar(
            SIDECAR_NAME,
            expected_digest=hashlib.sha256(payload).hexdigest(),
            expected_head_length=len(payload),
            expected_tail_length=0,
        )
    assert isinstance(caught.value.__cause__, OSError)


def test_verify_sidecar_rejects_a_directory(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / SIDECAR_NAME).mkdir()

    with pytest.raises(SidecarVerificationError, match="regular file"):
        directory.verify_sidecar(
            SIDECAR_NAME,
            expected_digest=hashlib.sha256(b"").hexdigest(),
            expected_head_length=0,
            expected_tail_length=0,
        )


def test_verify_sidecar_rejects_a_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    directory = _allocate(tmp_path)
    os.mkfifo(directory.path / SIDECAR_NAME)
    verifier = f"""
import hashlib
import sys
from pathlib import Path
from dr_store import DocumentDirectory, SidecarVerificationError

directory = DocumentDirectory(Path(sys.argv[1]), {MANIFEST_NAME!r})
try:
    directory.verify_sidecar(
        {SIDECAR_NAME!r},
        expected_digest=hashlib.sha256(b'').hexdigest(),
        expected_head_length=0,
        expected_tail_length=0,
    )
except SidecarVerificationError:
    print('rejected')
    raise SystemExit(0)
raise SystemExit('FIFO was accepted')
"""
    # The timeout is only a deadlock watchdog. Success is the child's typed
    # rejection and terminal outcome, never the passage of time.
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", verifier, str(directory.path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=WATCHDOG_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "rejected\n"


def test_verify_sidecar_streams_bounded_reads_from_the_inspected_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = bytes(range(256)) * 600
    replacement = b"x" * len(payload)
    directory = _allocate(tmp_path)
    sidecar_path = directory.path / SIDECAR_NAME
    sidecar_path.write_bytes(payload)
    real_open = os.open
    real_fstat = os.fstat
    real_read = os.read
    directory_descriptors: list[int] = []
    child_descriptors: list[int] = []
    inspected_descriptors: list[int] = []
    reads: list[tuple[int, int]] = []

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None:
            child_descriptors.append(descriptor)
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_NONBLOCK
            assert flags & os.O_CLOEXEC
        else:
            directory_descriptors.append(descriptor)
            assert flags & os.O_DIRECTORY
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_CLOEXEC
        return descriptor

    def recording_fstat(descriptor: int) -> os.stat_result:
        inspected_descriptors.append(descriptor)
        metadata = real_fstat(descriptor)
        sidecar_path.rename(directory.path / "opened-original.bin")
        sidecar_path.write_bytes(replacement)
        return metadata

    def recording_read(descriptor: int, size: int) -> bytes:
        reads.append((descriptor, size))
        return real_read(descriptor, size)

    monkeypatch.setattr(sidecar_module.os, "open", recording_open)
    monkeypatch.setattr(sidecar_module.os, "fstat", recording_fstat)
    monkeypatch.setattr(sidecar_module.os, "read", recording_read)
    directory.verify_sidecar(
        SIDECAR_NAME,
        expected_digest=hashlib.sha256(payload).hexdigest(),
        expected_head_length=len(payload),
        expected_tail_length=0,
    )

    assert len(child_descriptors) == 1
    assert inspected_descriptors == child_descriptors
    assert len(reads) >= 4
    assert {descriptor for descriptor, _ in reads} == set(child_descriptors)
    assert {size for _, size in reads} == {sidecar_module._READ_CHUNK_BYTES}
    assert sidecar_path.read_bytes() == replacement
    for descriptor in directory_descriptors + child_descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            real_fstat(descriptor)


def test_verify_sidecar_unreadable_child_is_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    (directory.path / SIDECAR_NAME).write_bytes(b"stored")
    real_open = os.open

    def refusing_child_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is not None:
            raise PermissionError("read refused")
        return real_open(path, flags, mode)

    monkeypatch.setattr(sidecar_module.os, "open", refusing_child_open)
    with pytest.raises(SidecarVerificationError) as caught:
        directory.verify_sidecar(
            SIDECAR_NAME,
            expected_digest=hashlib.sha256(b"stored").hexdigest(),
            expected_head_length=6,
            expected_tail_length=0,
        )
    assert isinstance(caught.value.__cause__, PermissionError)


def test_verify_sidecar_fails_closed_without_no_follow_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _allocate(tmp_path)
    (directory.path / SIDECAR_NAME).write_bytes(b"stored")
    monkeypatch.delattr(sidecar_module.os, "O_NOFOLLOW")

    with pytest.raises(SidecarVerificationError, match="atomic") as caught:
        directory.verify_sidecar(
            SIDECAR_NAME,
            expected_digest=hashlib.sha256(b"stored").hexdigest(),
            expected_head_length=6,
            expected_tail_length=0,
        )
    assert caught.value.__cause__ is None


def test_summary_is_frozen(tmp_path: Path) -> None:
    _, summary = _write(tmp_path, b"x", head_cap=None, tail_cap=None)
    with pytest.raises(AttributeError):
        summary.head_length = 99  # ty: ignore[invalid-assignment]


def test_sidecar_names_are_validated(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    for bad in ("..", ".", "", "nested/name", "esc\\ape", "nul\x00byte"):
        with pytest.raises(AllocationError):
            directory.open_sidecar(bad)


def test_unopenable_sidecar_is_typed(tmp_path: Path) -> None:
    directory = _allocate(tmp_path)
    (directory.path / SIDECAR_NAME).mkdir()
    with pytest.raises(AllocationError) as caught:
        directory.open_sidecar(SIDECAR_NAME)
    assert isinstance(caught.value.__cause__, OSError)


def test_a_failed_flush_is_typed_and_closes_the_handle(
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
    # The handle is closed even though the flush failed, so the file
    # descriptor cannot leak and no further byte can reach the file.
    with pytest.raises(AllocationError) as rejected:
        writer.write(b"after")
    assert isinstance(rejected.value.__cause__, ValueError)
