from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from dr_store import DocumentDirectory, SidecarVerificationError
from dr_store.document_directory import sidecar as sidecar_module

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST_NAME = "record.json"
MANIFEST_MAX_BYTES = 1 << 20
SIDECAR_NAME = "stdout.bin"
WATCHDOG_SECONDS = 60


def _allocate(root: Path) -> DocumentDirectory:
    return DocumentDirectory.allocate(
        root,
        prefix="run",
        manifest_name=MANIFEST_NAME,
        manifest_max_bytes=MANIFEST_MAX_BYTES,
    )


def test_verify_sidecar_accepts_matching_bytes(tmp_path: Path) -> None:
    payload = b"headtail"
    directory = _allocate(tmp_path)
    (directory.path / SIDECAR_NAME).write_bytes(payload)

    directory.verify_sidecar(
        SIDECAR_NAME,
        expected_digest=hashlib.sha256(payload).hexdigest(),
        expected_head_length=4,
        expected_tail_length=4,
    )


def test_verify_sidecar_rejects_mutated_bytes(tmp_path: Path) -> None:
    payload = bytearray(b"headtail")
    expected_digest = hashlib.sha256(payload).hexdigest()
    payload[2] ^= 0xFF
    directory = _allocate(tmp_path)
    (directory.path / SIDECAR_NAME).write_bytes(payload)

    with pytest.raises(SidecarVerificationError):
        directory.verify_sidecar(
            SIDECAR_NAME,
            expected_digest=expected_digest,
            expected_head_length=4,
            expected_tail_length=4,
        )


@pytest.mark.parametrize(
    ("payload", "expected_head_length", "expected_tail_length"),
    [(b"headtail", 5, 4), (b"headtai", 4, 4)],
    ids=["mismatched-expectation", "truncated-file"],
)
def test_verify_sidecar_rejects_mismatched_length(
    tmp_path: Path,
    payload: bytes,
    expected_head_length: int,
    expected_tail_length: int,
) -> None:
    directory = _allocate(tmp_path)
    (directory.path / SIDECAR_NAME).write_bytes(payload)

    with pytest.raises(SidecarVerificationError):
        directory.verify_sidecar(
            SIDECAR_NAME,
            expected_digest=hashlib.sha256(b"headtail").hexdigest(),
            expected_head_length=expected_head_length,
            expected_tail_length=expected_tail_length,
        )


@pytest.mark.parametrize(
    ("expected_head_length", "expected_tail_length", "role"),
    [
        pytest.param(-1, 2, "expected_head_length", id="negative-head"),
        pytest.param(2, -1, "expected_tail_length", id="negative-tail"),
    ],
)
def test_verify_sidecar_rejects_negative_segment_length(
    tmp_path: Path,
    expected_head_length: int,
    expected_tail_length: int,
    role: str,
) -> None:
    payload = b"x"
    directory = _allocate(tmp_path)
    (directory.path / SIDECAR_NAME).write_bytes(payload)

    with pytest.raises(SidecarVerificationError, match=role):
        directory.verify_sidecar(
            SIDECAR_NAME,
            expected_digest=hashlib.sha256(payload).hexdigest(),
            expected_head_length=expected_head_length,
            expected_tail_length=expected_tail_length,
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
        ".dr-store-document-owned",
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

    with pytest.raises(SidecarVerificationError):
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

directory = DocumentDirectory(
    Path(sys.argv[1]),
    {MANIFEST_NAME!r},
    manifest_max_bytes={MANIFEST_MAX_BYTES!r},
)
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
        with pytest.raises(
            OSError,
            check=lambda exc: exc.errno == errno.EBADF,
        ):
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

    with pytest.raises(SidecarVerificationError) as caught:
        directory.verify_sidecar(
            SIDECAR_NAME,
            expected_digest=hashlib.sha256(b"stored").hexdigest(),
            expected_head_length=6,
            expected_tail_length=0,
        )
    assert caught.value.__cause__ is None
