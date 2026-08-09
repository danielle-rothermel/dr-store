from __future__ import annotations

import errno
import hashlib
import inspect
import os
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from dr_serialize import canonical_json_bytes, validate_strict_json

from dr_store import (
    ArtifactBundlePublication,
    ArtifactBundleReader,
    BundleIncompleteError,
    BundleReadError,
    BundleReadLimits,
    BundleVerificationError,
    BundleVerificationReason,
    VerifyingArtifactReader,
)
from dr_store.artifact_bundle import reading as reading_module

if TYPE_CHECKING:
    from collections.abc import Callable

    from dr_serialize import Jsonable

MANIFEST_MAX_BYTES = 1 << 20
WATCHDOG_SECONDS = 60
LIMITS = BundleReadLimits(
    manifest_max_bytes=MANIFEST_MAX_BYTES,
    manifest_max_depth=64,
    max_artifacts=16,
    max_bytes_per_artifact=1 << 20,
    max_total_artifact_bytes=4 << 20,
)


def _publish(
    root: Path,
    artifacts: tuple[tuple[str, bytes], ...] = (("stdout.bin", b"output"),),
    *,
    payload: Jsonable = None,
) -> ArtifactBundlePublication:
    publication = ArtifactBundlePublication.allocate(root, prefix="run")
    for name, content in artifacts:
        writer = publication.open_artifact(name)
        writer.write(content)
        writer.finalize()
    publication.publish(payload)
    return publication


def _reader(
    path: Path,
    *,
    limits: BundleReadLimits = LIMITS,
) -> ArtifactBundleReader:
    return ArtifactBundleReader(path, limits=limits)


def _manifest_bytes(
    artifacts: list[Jsonable],
    *,
    payload: Jsonable = None,
    format_marker: str = "dr-store-artifact-bundle-v1",
    extra: dict[str, Jsonable] | None = None,
) -> bytes:
    manifest: dict[str, Jsonable] = {
        "format": format_marker,
        "artifacts": artifacts,
        "payload": payload,
    }
    if extra is not None:
        manifest.update(extra)
    return canonical_json_bytes(validate_strict_json(manifest))


def _descriptor(name: str, content: bytes) -> dict[str, Jsonable]:
    return {
        "name": name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content),
    }


def _consume_nothing(_reader: VerifyingArtifactReader) -> None:
    return


def _consume_zero(reader: VerifyingArtifactReader) -> None:
    reader.read(0)


def _consume_one(reader: VerifyingArtifactReader) -> None:
    reader.read(1)


def _consume_exact_without_eof(reader: VerifyingArtifactReader) -> None:
    reader.read(len(b"output"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_max_bytes", -1),
        ("manifest_max_bytes", True),
        ("manifest_max_depth", -1),
        ("manifest_max_depth", 1.5),
        ("max_artifacts", -1),
        ("max_artifacts", False),
        ("max_bytes_per_artifact", -1),
        ("max_bytes_per_artifact", "10"),
        ("max_total_artifact_bytes", -1),
        ("max_total_artifact_bytes", None),
    ],
)
def test_read_limits_are_frozen_slotted_and_validate_exact_integers(
    field: str,
    value: object,
) -> None:
    arguments: dict[str, object] = {
        item.name: getattr(LIMITS, item.name) for item in fields(LIMITS)
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=field):
        BundleReadLimits(**arguments)  # ty: ignore[invalid-argument-type]

    assert not hasattr(LIMITS, "__dict__")
    with pytest.raises(AttributeError):
        LIMITS.manifest_max_bytes = 0


def test_reader_requires_explicit_limits_instance(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="BundleReadLimits"):
        ArtifactBundleReader(
            tmp_path,
            limits=None,  # ty: ignore[invalid-argument-type]
        )


def test_audit_verifies_every_declared_artifact_and_ignores_other_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(
        tmp_path,
        (("a.bin", b"a"), ("b.bin", b"bb"), ("c.bin", b"ccc")),
        payload={"kind": "example"},
    )
    (publication.path / "unrelated.bin").write_bytes(b"not in manifest")
    original_open = reading_module.os.open
    opened_children: list[str] = []

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is not None:
            opened_children.append(os.fsdecode(path))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(reading_module.os, "open", recording_open)
    manifest = _reader(publication.path).audit()

    assert [item.name for item in manifest.artifacts] == [
        "a.bin",
        "b.bin",
        "c.bin",
    ]
    assert opened_children == ["manifest.json", "a.bin", "b.bin", "c.bin"]
    assert "unrelated.bin" not in opened_children


def test_verified_consumption_reads_selected_content_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = bytes(range(256)) * 700
    publication = _publish(tmp_path, (("payload.bin", content),))
    original_open = reading_module.os.open
    original_read = reading_module.os.read
    artifact_descriptors: list[int] = []
    artifact_reads: list[bytes] = []
    observed = bytearray()

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "payload.bin":
            artifact_descriptors.append(descriptor)
        return descriptor

    def recording_read(descriptor: int, size: int) -> bytes:
        chunk = original_read(descriptor, size)
        if descriptor in artifact_descriptors:
            artifact_reads.append(chunk)
        return chunk

    def consume(reader: VerifyingArtifactReader) -> None:
        while chunk := reader.read(7919):
            observed.extend(chunk)

    monkeypatch.setattr(reading_module.os, "open", recording_open)
    monkeypatch.setattr(reading_module.os, "read", recording_read)
    descriptor = _reader(publication.path).consume_and_verify_artifact(
        "payload.bin",
        consume,
    )

    assert len(artifact_descriptors) == 1
    assert b"".join(artifact_reads) == content
    assert artifact_reads[-1] == b""
    assert observed == content
    assert descriptor == _reader(publication.path).audit().artifacts[0]


@pytest.mark.parametrize(
    "consumer",
    [
        pytest.param(_consume_nothing, id="no-read"),
        pytest.param(_consume_zero, id="zero-read"),
        pytest.param(_consume_one, id="early-return"),
        pytest.param(
            _consume_exact_without_eof,
            id="exact-length-without-eof-read",
        ),
    ],
)
def test_verified_consumption_requires_observed_os_eof(
    tmp_path: Path,
    consumer: Callable[[VerifyingArtifactReader], None],
) -> None:
    publication = _publish(tmp_path)
    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path).consume_and_verify_artifact(
            "stdout.bin",
            consumer,
        )
    assert caught.value.artifact_name == "stdout.bin"
    assert (
        caught.value.reason is BundleVerificationReason.INCOMPLETE_CONSUMPTION
    )


def test_exact_length_then_empty_read_proves_eof(tmp_path: Path) -> None:
    publication = _publish(tmp_path)
    reads: list[bytes] = []

    def consume(reader: VerifyingArtifactReader) -> None:
        reads.append(reader.read(len(b"output")))
        reads.append(reader.read(1))

    _reader(publication.path).consume_and_verify_artifact(
        "stdout.bin",
        consume,
    )
    assert reads == [b"output", b""]


def test_read_all_proves_eof_by_reading_through_empty_os_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    original_read = reading_module.os.read
    returned: list[bytes] = []

    def recording_read(descriptor: int, size: int) -> bytes:
        chunk = original_read(descriptor, size)
        returned.append(chunk)
        return chunk

    monkeypatch.setattr(reading_module.os, "read", recording_read)
    observed: list[bytes] = []
    _reader(publication.path).consume_and_verify_artifact(
        "stdout.bin",
        lambda reader: observed.append(reader.read()),
    )
    assert observed == [b"output"]
    assert returned[-1] == b""


def test_consumer_is_called_once_and_facade_is_invalid_after_callback(
    tmp_path: Path,
) -> None:
    publication = _publish(tmp_path)
    captured: list[VerifyingArtifactReader] = []

    def consume(reader: VerifyingArtifactReader) -> None:
        captured.append(reader)
        assert reader.read() == b"output"

    _reader(publication.path).consume_and_verify_artifact(
        "stdout.bin",
        consume,
    )
    assert len(captured) == 1
    with pytest.raises(BundleReadError, match="no longer active"):
        captured[0].read()


def test_consumer_exception_is_preserved_as_cause(tmp_path: Path) -> None:
    publication = _publish(tmp_path)
    failure = RuntimeError("consumer refused")
    calls = 0
    captured: list[VerifyingArtifactReader] = []

    def consume(reader: VerifyingArtifactReader) -> None:
        nonlocal calls
        calls += 1
        captured.append(reader)
        raise failure

    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).consume_and_verify_artifact(
            "stdout.bin",
            consume,
        )
    assert calls == 1
    assert caught.value.__cause__ is failure
    with pytest.raises(BundleReadError, match="no longer active"):
        captured[0].read()


def test_nested_bundle_read_failure_is_translated_to_outer_bundle(
    tmp_path: Path,
) -> None:
    publication = _publish(tmp_path)
    nested_bundle = tmp_path / "nested-bundle"
    nested_bundle.mkdir()
    nested_failure: BundleReadError | None = None

    def consume(_facade: VerifyingArtifactReader) -> None:
        nonlocal nested_failure
        try:
            _reader(nested_bundle).audit()
        except BundleReadError as exc:
            nested_failure = exc
            raise

    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).consume_and_verify_artifact(
            "stdout.bin",
            consume,
        )
    assert type(caught.value) is BundleReadError
    assert caught.value.path == publication.path
    assert caught.value.__cause__ is nested_failure
    assert nested_failure is not None
    assert nested_failure.path == nested_bundle


def test_facade_read_failure_preserves_storage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    original_open = reading_module.os.open
    original_read = reading_module.os.read
    artifact_descriptor: int | None = None
    read_failure = OSError(errno.EIO, "injected artifact read failure")

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal artifact_descriptor
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "stdout.bin":
            artifact_descriptor = descriptor
        return descriptor

    def failing_read(descriptor: int, size: int) -> bytes:
        if descriptor == artifact_descriptor:
            raise read_failure
        return original_read(descriptor, size)

    def consume(reader: VerifyingArtifactReader) -> None:
        reader.read()

    monkeypatch.setattr(reading_module.os, "open", recording_open)
    monkeypatch.setattr(reading_module.os, "read", failing_read)
    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).consume_and_verify_artifact(
            "stdout.bin",
            consume,
        )
    assert type(caught.value) is BundleReadError
    assert caught.value.path == publication.path
    assert caught.value.__cause__ is read_failure


@pytest.mark.parametrize(
    "raw",
    [
        b'{"artifacts":[],"format":"unknown","payload":null}',
        b'{"artifacts":[],"format":"dr-store-artifact-bundle-v1",'
        b'"payload":null,"unknown":true}',
        b'{"artifacts":[],"format":"dr-store-artifact-bundle-v1",'
        b'"payload":null,"payload":true}',
        b'{"artifacts": [], "format":"dr-store-artifact-bundle-v1",'
        b'"payload":null}',
        _manifest_bytes([_descriptor("b", b"b"), _descriptor("a", b"a")]),
        _manifest_bytes([_descriptor("a", b"a"), _descriptor("a", b"a")]),
    ],
    ids=[
        "unknown-format",
        "unknown-field",
        "duplicate-key",
        "noncanonical-spacing",
        "unsorted-artifacts",
        "duplicate-artifacts",
    ],
)
def test_invalid_terminal_manifest_is_incomplete(
    tmp_path: Path,
    raw: bytes,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_bytes(raw)
    with pytest.raises(BundleIncompleteError) as caught:
        _reader(bundle).audit()
    assert caught.value.path == bundle
    assert caught.value.__cause__ is not None


def test_missing_manifest_is_incomplete_with_os_cause(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with pytest.raises(BundleIncompleteError) as caught:
        _reader(bundle).audit()
    assert isinstance(caught.value.__cause__, OSError)


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_nonregular_manifest_is_incomplete(
    tmp_path: Path,
    kind: str,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = bundle / "manifest.json"
    if kind == "directory":
        manifest.mkdir()
    else:
        target = tmp_path / "outside.json"
        target.write_bytes(_manifest_bytes([]))
        manifest.symlink_to(target)
    with pytest.raises(BundleIncompleteError) as caught:
        _reader(bundle).audit()
    assert isinstance(caught.value.__cause__, OSError)


def test_manifest_read_io_failure_is_a_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    original_open = reading_module.os.open
    original_read = reading_module.os.read
    manifest_descriptor: int | None = None
    read_failure = OSError(errno.EIO, "injected manifest read failure")

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal manifest_descriptor
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "manifest.json":
            manifest_descriptor = descriptor
        return descriptor

    def failing_read(descriptor: int, size: int) -> bytes:
        if descriptor == manifest_descriptor:
            raise read_failure
        return original_read(descriptor, size)

    monkeypatch.setattr(reading_module.os, "open", recording_open)
    monkeypatch.setattr(reading_module.os, "read", failing_read)
    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).audit()
    assert type(caught.value) is BundleReadError
    assert caught.value.path == publication.path
    assert caught.value.__cause__ is read_failure


def test_manifest_close_io_failure_is_a_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    original_open = reading_module.os.open
    original_close = reading_module.os.close
    manifest_descriptor: int | None = None
    close_failure = OSError(errno.EIO, "injected manifest close failure")

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal manifest_descriptor
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "manifest.json":
            manifest_descriptor = descriptor
        return descriptor

    def failing_close(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor == manifest_descriptor:
            raise close_failure

    monkeypatch.setattr(reading_module.os, "open", recording_open)
    monkeypatch.setattr(reading_module.os, "close", failing_close)
    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).audit()
    assert type(caught.value) is BundleReadError
    assert caught.value.path == publication.path
    assert caught.value.__cause__ is close_failure


def test_manifest_byte_and_depth_bounds_are_enforced(tmp_path: Path) -> None:
    publication = _publish(tmp_path, payload={"nested": [[None]]})
    manifest_bytes = (publication.path / "manifest.json").read_bytes()

    byte_limits = BundleReadLimits(
        manifest_max_bytes=len(manifest_bytes) - 1,
        manifest_max_depth=64,
        max_artifacts=16,
        max_bytes_per_artifact=1 << 20,
        max_total_artifact_bytes=4 << 20,
    )
    with pytest.raises(BundleIncompleteError):
        _reader(publication.path, limits=byte_limits).audit()

    depth_limits = BundleReadLimits(
        manifest_max_bytes=MANIFEST_MAX_BYTES,
        manifest_max_depth=0,
        max_artifacts=16,
        max_bytes_per_artifact=1 << 20,
        max_total_artifact_bytes=4 << 20,
    )
    with pytest.raises(BundleIncompleteError):
        _reader(publication.path, limits=depth_limits).audit()


@pytest.mark.parametrize(
    ("artifacts", "limits", "failing_name"),
    [
        (
            (("a", b"a"), ("b", b"b")),
            BundleReadLimits(
                manifest_max_bytes=MANIFEST_MAX_BYTES,
                manifest_max_depth=64,
                max_artifacts=1,
                max_bytes_per_artifact=10,
                max_total_artifact_bytes=10,
            ),
            "b",
        ),
        (
            (("large", b"1234"),),
            BundleReadLimits(
                manifest_max_bytes=MANIFEST_MAX_BYTES,
                manifest_max_depth=64,
                max_artifacts=1,
                max_bytes_per_artifact=3,
                max_total_artifact_bytes=10,
            ),
            "large",
        ),
        (
            (("a", b"aa"), ("b", b"bb")),
            BundleReadLimits(
                manifest_max_bytes=MANIFEST_MAX_BYTES,
                manifest_max_depth=64,
                max_artifacts=2,
                max_bytes_per_artifact=4,
                max_total_artifact_bytes=3,
            ),
            "b",
        ),
    ],
    ids=["artifact-count", "per-artifact-bytes", "total-artifact-bytes"],
)
def test_declared_artifact_bounds_fail_before_artifact_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifacts: tuple[tuple[str, bytes], ...],
    limits: BundleReadLimits,
    failing_name: str,
) -> None:
    publication = _publish(tmp_path, artifacts)
    original_open = reading_module.os.open
    opened: list[str] = []

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is not None:
            opened.append(os.fsdecode(path))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(reading_module.os, "open", recording_open)
    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path, limits=limits).audit()
    assert caught.value.artifact_name == failing_name
    assert caught.value.reason is BundleVerificationReason.BOUNDS_EXCEEDED
    assert opened == ["manifest.json"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", BundleVerificationReason.MISSING),
        ("directory", BundleVerificationReason.NOT_REGULAR),
        ("symlink", BundleVerificationReason.NOT_REGULAR),
        ("length", BundleVerificationReason.MISMATCH),
        ("hash", BundleVerificationReason.MISMATCH),
    ],
)
def test_audit_reports_exact_artifact_verification_reason(
    tmp_path: Path,
    mutation: str,
    reason: BundleVerificationReason,
) -> None:
    publication = _publish(tmp_path)
    artifact = publication.path / "stdout.bin"
    original = artifact.read_bytes()
    if mutation == "missing":
        artifact.unlink()
    elif mutation == "directory":
        artifact.unlink()
        artifact.mkdir()
    elif mutation == "symlink":
        artifact.unlink()
        target = tmp_path / "outside.bin"
        target.write_bytes(original)
        artifact.symlink_to(target)
    elif mutation == "length":
        artifact.write_bytes(original + b"x")
    else:
        artifact.write_bytes(b"x" * len(original))

    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path).audit()
    assert caught.value.artifact_name == "stdout.bin"
    assert caught.value.reason is reason
    if mutation in {"missing", "symlink"}:
        assert isinstance(caught.value.__cause__, OSError)


def test_unknown_selected_artifact_is_missing_without_child_open(
    tmp_path: Path,
) -> None:
    publication = _publish(tmp_path)
    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path).consume_and_verify_artifact(
            "unknown.bin",
            _consume_nothing,
        )
    assert caught.value.artifact_name == "unknown.bin"
    assert caught.value.reason is BundleVerificationReason.MISSING


def test_consumption_delivers_mutated_bytes_then_reports_mismatch(
    tmp_path: Path,
) -> None:
    publication = _publish(tmp_path)
    mutated = b"x" * len(b"output")
    (publication.path / "stdout.bin").write_bytes(mutated)
    observed: list[bytes] = []

    def consume(reader: VerifyingArtifactReader) -> None:
        observed.append(reader.read())

    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path).consume_and_verify_artifact(
            "stdout.bin",
            consume,
        )
    assert observed == [mutated]
    assert caught.value.reason is BundleVerificationReason.MISMATCH


def test_fifo_artifact_is_rejected_without_blocking(tmp_path: Path) -> None:
    publication = _publish(tmp_path)
    artifact = publication.path / "stdout.bin"
    artifact.unlink()
    os.mkfifo(artifact)
    script = f"""
from dr_store import (
    ArtifactBundleReader,
    BundleReadLimits,
    BundleVerificationError,
)
reader = ArtifactBundleReader(
    {str(publication.path)!r},
    limits=BundleReadLimits(
        manifest_max_bytes={MANIFEST_MAX_BYTES},
        manifest_max_depth=64,
        max_artifacts=16,
        max_bytes_per_artifact={1 << 20},
        max_total_artifact_bytes={4 << 20},
    ),
)
try:
    reader.audit()
except BundleVerificationError:
    print('rejected')
    raise SystemExit(0)
raise SystemExit('FIFO was accepted')
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=WATCHDOG_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "rejected\n"


def test_directory_descriptor_stays_pinned_across_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path, (("payload.bin", b"original"),))
    bundle_path = publication.path
    moved_path = tmp_path / "opened-original"
    original_open = reading_module.os.open
    replaced = False

    def replacing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if (
            dir_fd is None
            and Path(os.fsdecode(path)) == bundle_path
            and not replaced
        ):
            replaced = True
            bundle_path.rename(moved_path)
            bundle_path.mkdir()
            attacker = b"attacker"
            (bundle_path / "payload.bin").write_bytes(attacker)
            (bundle_path / "manifest.json").write_bytes(
                _manifest_bytes([_descriptor("payload.bin", attacker)])
            )
        return descriptor

    monkeypatch.setattr(reading_module.os, "open", replacing_open)
    manifest = _reader(bundle_path).audit()
    assert (
        manifest.artifacts[0].sha256 == hashlib.sha256(b"original").hexdigest()
    )
    assert (bundle_path / "payload.bin").read_bytes() == b"attacker"


def test_artifact_descriptor_stays_pinned_across_child_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"opened descriptor bytes"
    replacement = b"x" * len(original)
    assert len(original) == len(replacement)
    publication = _publish(tmp_path, (("payload.bin", original),))
    artifact = publication.path / "payload.bin"
    original_open = reading_module.os.open
    original_fstat = reading_module.os.fstat
    child_descriptor: int | None = None
    replaced = False

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal child_descriptor
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "payload.bin":
            child_descriptor = descriptor
        return descriptor

    def replacing_fstat(descriptor: int) -> os.stat_result:
        nonlocal replaced
        metadata = original_fstat(descriptor)
        if descriptor == child_descriptor and not replaced:
            replaced = True
            artifact.rename(publication.path / "opened-original.bin")
            artifact.write_bytes(replacement)
        return metadata

    monkeypatch.setattr(reading_module.os, "open", recording_open)
    monkeypatch.setattr(reading_module.os, "fstat", replacing_fstat)
    observed: list[bytes] = []
    _reader(publication.path).consume_and_verify_artifact(
        "payload.bin",
        lambda reader: observed.append(reader.read()),
    )
    assert observed == [original]
    assert artifact.read_bytes() == replacement


def test_all_read_descriptors_close_on_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    original_open = reading_module.os.open
    original_fstat = reading_module.os.fstat
    opened: list[int] = []

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(reading_module.os, "open", recording_open)
    with pytest.raises(BundleVerificationError):
        _reader(publication.path).consume_and_verify_artifact(
            "stdout.bin",
            _consume_one,
        )
    for descriptor in opened:
        with pytest.raises(
            OSError,
            check=lambda exc: exc.errno == errno.EBADF,
        ):
            original_fstat(descriptor)


def test_reader_fails_closed_without_required_descriptor_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    monkeypatch.delattr(reading_module.os, "O_NOFOLLOW")
    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).audit()
    assert isinstance(caught.value.__cause__, OSError)


def test_reader_fails_closed_without_dir_fd_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    monkeypatch.setattr(reading_module, "_OPEN_SUPPORTS_DIR_FD", False)
    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).audit()
    assert isinstance(caught.value.__cause__, OSError)


def test_consumption_uses_total_bound_when_it_is_lower_than_per_file_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path, (("payload.bin", b"abc"),))
    (publication.path / "payload.bin").write_bytes(b"abcde")
    limits = BundleReadLimits(
        manifest_max_bytes=MANIFEST_MAX_BYTES,
        manifest_max_depth=64,
        max_artifacts=1,
        max_bytes_per_artifact=100,
        max_total_artifact_bytes=3,
    )
    original_read = reading_module.os.read
    artifact_descriptor: int | None = None
    artifact_reads = 0
    original_open = reading_module.os.open

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal artifact_descriptor
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "payload.bin":
            artifact_descriptor = descriptor
        return descriptor

    def recording_read(descriptor: int, size: int) -> bytes:
        nonlocal artifact_reads
        if descriptor == artifact_descriptor:
            artifact_reads += 1
        return original_read(descriptor, size)

    monkeypatch.setattr(reading_module.os, "open", recording_open)
    monkeypatch.setattr(reading_module.os, "read", recording_read)
    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path, limits=limits).consume_and_verify_artifact(
            "payload.bin",
            _consume_nothing,
        )
    assert caught.value.reason is BundleVerificationReason.BOUNDS_EXCEEDED
    assert artifact_reads == 0


def test_reader_and_facade_public_surfaces_are_exact() -> None:
    assert {
        name for name in dir(ArtifactBundleReader) if not name.startswith("_")
    } == {"audit", "consume_and_verify_artifact"}
    assert {
        name
        for name in dir(VerifyingArtifactReader)
        if not name.startswith("_")
    } == {"read"}
    assert list(inspect.signature(ArtifactBundleReader).parameters) == [
        "path",
        "limits",
    ]
    assert list(
        inspect.signature(
            ArtifactBundleReader.consume_and_verify_artifact
        ).parameters
    ) == ["self", "name", "consumer"]
    with pytest.raises(TypeError, match="provided only during"):
        VerifyingArtifactReader()


def test_verification_error_exposes_exact_structured_fields(
    tmp_path: Path,
) -> None:
    publication = _publish(tmp_path)
    (publication.path / "stdout.bin").unlink()
    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path).audit()
    assert caught.value.path == publication.path
    assert caught.value.artifact_name == "stdout.bin"
    assert caught.value.reason is BundleVerificationReason.MISSING
    assert vars(caught.value) == {
        "path": publication.path,
        "artifact_name": "stdout.bin",
        "reason": BundleVerificationReason.MISSING,
    }
