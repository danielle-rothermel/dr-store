from __future__ import annotations

import errno
import hashlib
import inspect
import os
import subprocess
import sys
from dataclasses import fields
from typing import TYPE_CHECKING

import pytest
from dr_serialize import canonical_json_bytes, validate_strict_json

from dr_store import (
    ArtifactBundlePublication,
    ArtifactBundleReader,
    ArtifactDescriptor,
    BundleIncompleteError,
    BundleReadError,
    BundleReadLimits,
    BundleVerificationError,
    BundleVerificationReason,
    DocumentFileError,
    RegularChildFailureReason,
    VerifiedRegularChildReadError,
    VerifyingArtifactReader,
)
from dr_store.artifact_bundle import reading as reading_module
from dr_store.core import descriptor_io as descriptor_io_module
from dr_store.core import filesystem as filesystem_module
from dr_store.core import verified_read as verified_read_module
from dr_store.document_file import canonical_json as document_file_module

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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


def _record_child_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Record every directory-relative child name opened during a read."""
    opened: list[str] = []
    original_open_child = descriptor_io_module.open_child_descriptor

    def recording_open_child(
        name: str,
        *,
        flags: int,
        directory_descriptor: int,
    ) -> int:
        opened.append(name)
        return original_open_child(
            name,
            flags=flags,
            directory_descriptor=directory_descriptor,
        )

    for module in (
        verified_read_module,
        document_file_module,
    ):
        monkeypatch.setattr(
            module,
            "open_child_descriptor",
            recording_open_child,
        )
    return opened


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
    opened_children = _record_child_opens(monkeypatch)
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
    opened_children = _record_child_opens(monkeypatch)
    observed = bytearray()

    def consume(reader: VerifyingArtifactReader) -> None:
        while chunk := reader.read(7919):
            observed.extend(chunk)

    descriptor = _reader(publication.path).consume_and_verify_artifact(
        "payload.bin",
        consume,
    )

    assert opened_children == ["manifest.json", "payload.bin"]
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
def test_verified_consumption_requires_observed_eof(
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


def test_read_all_delivers_the_complete_verified_artifact(
    tmp_path: Path,
) -> None:
    publication = _publish(tmp_path)
    observed: list[bytes] = []
    _reader(publication.path).consume_and_verify_artifact(
        "stdout.bin",
        lambda reader: observed.append(reader.read()),
    )
    assert observed == [b"output"]


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


@pytest.mark.parametrize(
    "raw",
    [
        b'{"artifacts":[],"format":"unknown","payload":null}',
        (
            b'{"artifacts":[],"format":"dr-store-artifact-bundle-v1",'
            b'"payload":null,"unknown":true}'
        ),
        (
            b'{"artifacts":[],"format":"dr-store-artifact-bundle-v1",'
            b'"payload":null,"payload":true}'
        ),
        (
            b'{"artifacts": [], "format":"dr-store-artifact-bundle-v1",'
            b'"payload":null}'
        ),
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


def test_missing_bundle_directory_is_a_read_error(tmp_path: Path) -> None:
    with pytest.raises(BundleReadError) as caught:
        _reader(tmp_path / "absent").audit()
    assert type(caught.value) is BundleReadError
    assert caught.value.path == tmp_path / "absent"


def test_manifest_read_io_failure_is_a_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    read_failure = OSError(errno.EIO, "injected manifest read failure")

    def failing_read(*_args: object, **_kwargs: object) -> bytes:
        raise read_failure

    monkeypatch.setattr(
        document_file_module,
        "read_bounded_child_descriptor",
        failing_read,
    )
    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).audit()
    assert type(caught.value) is BundleReadError
    assert caught.value.path == publication.path
    assert caught.value.__cause__ is read_failure


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
    opened = _record_child_opens(monkeypatch)
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


def test_unknown_selected_artifact_is_missing_without_child_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    opened = _record_child_opens(monkeypatch)
    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path).consume_and_verify_artifact(
            "unknown.bin",
            _consume_nothing,
        )
    assert caught.value.artifact_name == "unknown.bin"
    assert caught.value.reason is BundleVerificationReason.MISSING
    assert opened == ["manifest.json"]


def test_consumption_refuses_mutated_bytes_before_delivering_them(
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
    assert observed == []
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


def test_directory_is_pinned_within_one_verified_child_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path, (("payload.bin", b"original"),))
    bundle_path = publication.path
    moved_path = tmp_path / "opened-original"
    original_open_directory = verified_read_module.open_directory_descriptor
    replaced = False

    def replacing_open_directory(directory: Path, *, flags: int) -> int:
        nonlocal replaced
        descriptor = original_open_directory(directory, flags=flags)
        if not replaced:
            replaced = True
            bundle_path.rename(moved_path)
            bundle_path.mkdir()
            (bundle_path / "payload.bin").write_bytes(b"attacker")
        return descriptor

    monkeypatch.setattr(
        verified_read_module,
        "open_directory_descriptor",
        replacing_open_directory,
    )
    observed: list[bytes] = []
    _reader(bundle_path).consume_and_verify_artifact(
        "payload.bin",
        lambda reader: observed.append(reader.read()),
    )
    assert observed == [b"original"]
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
    original_open_child = verified_read_module.open_child_descriptor
    original_fstat = verified_read_module.os.fstat
    artifact_descriptor: int | None = None

    def recording_open_child(
        name: str,
        *,
        flags: int,
        directory_descriptor: int,
    ) -> int:
        nonlocal artifact_descriptor
        descriptor = original_open_child(
            name,
            flags=flags,
            directory_descriptor=directory_descriptor,
        )
        if name == "payload.bin":
            artifact_descriptor = descriptor
        return descriptor

    def replacing_fstat(descriptor: int) -> os.stat_result:
        nonlocal artifact_descriptor
        metadata = original_fstat(descriptor)
        if descriptor == artifact_descriptor:
            artifact_descriptor = None
            artifact.rename(publication.path / "opened-original.bin")
            artifact.write_bytes(replacement)
        return metadata

    monkeypatch.setattr(
        verified_read_module,
        "open_child_descriptor",
        recording_open_child,
    )
    monkeypatch.setattr(verified_read_module.os, "fstat", replacing_fstat)
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
    opened: list[int] = []
    original_fstat = os.fstat

    def record(module: object, attribute: str) -> None:
        original = getattr(module, attribute)

        def recording(*args: object, **kwargs: object) -> int:
            descriptor = original(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        monkeypatch.setattr(module, attribute, recording)

    for module in (verified_read_module, document_file_module):
        record(module, "open_directory_descriptor")
        record(module, "open_child_descriptor")

    with pytest.raises(BundleVerificationError):
        _reader(publication.path).consume_and_verify_artifact(
            "stdout.bin",
            _consume_one,
        )
    assert opened
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
    monkeypatch.setattr(
        filesystem_module,
        "_pinned_read_support_detail",
        lambda: "O_NOFOLLOW",
    )
    monkeypatch.setattr(
        verified_read_module,
        "_pinned_read_support_detail",
        lambda: "O_NOFOLLOW",
    )
    monkeypatch.setattr(
        document_file_module,
        "_pinned_read_support_detail",
        lambda: "O_NOFOLLOW",
    )
    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).audit()
    assert isinstance(caught.value.__cause__, OSError)


def test_consumption_fails_closed_without_required_descriptor_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    for module in (
        filesystem_module,
        verified_read_module,
        document_file_module,
    ):
        monkeypatch.setattr(
            module,
            "_pinned_read_support_detail",
            lambda: "O_NOFOLLOW",
        )
    with pytest.raises(BundleReadError) as caught:
        _reader(publication.path).consume_and_verify_artifact(
            "stdout.bin",
            _consume_nothing,
        )
    assert type(caught.value) is BundleReadError
    assert isinstance(caught.value.__cause__, OSError)


def test_unsupported_platform_artifact_read_is_not_a_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path)
    monkeypatch.setattr(
        verified_read_module,
        "_pinned_read_support_detail",
        lambda: "os.open(dir_fd=...)",
    )
    with pytest.raises(BundleReadError) as caught:
        reading_module._read_verified_artifact(
            publication.path,
            ArtifactDescriptor(
                name="stdout.bin",
                sha256=hashlib.sha256(b"output").hexdigest(),
                byte_length=len(b"output"),
            ),
            max_bytes=LIMITS.max_bytes_per_artifact,
        )
    assert type(caught.value) is BundleReadError
    cause = caught.value.__cause__
    assert isinstance(cause, VerifiedRegularChildReadError)
    assert cause.reason is RegularChildFailureReason.UNSUPPORTED_PLATFORM
    assert (
        RegularChildFailureReason.UNSUPPORTED_PLATFORM
        not in reading_module._VERIFICATION_REASON_BY_CHILD_REASON
    )


@pytest.mark.parametrize("existing_bytes", [None, b"not a directory"])
def test_unopenable_bundle_directory_reports_the_innermost_cause(
    tmp_path: Path,
    existing_bytes: bytes | None,
) -> None:
    bundle_path = tmp_path / "bundle"
    if existing_bytes is not None:
        bundle_path.write_bytes(existing_bytes)

    with pytest.raises(BundleReadError) as caught:
        _reader(bundle_path).audit()

    assert type(caught.value) is BundleReadError
    cause = caught.value.__cause__
    # The chain is AllocationError <- DocumentFileError with nothing beneath
    # it, so the innermost layering error is reported, not the outer wrapper.
    assert type(cause) is DocumentFileError
    assert cause.__cause__ is None


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
    opened = _record_child_opens(monkeypatch)
    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path, limits=limits).consume_and_verify_artifact(
            "payload.bin",
            _consume_nothing,
        )
    assert caught.value.reason is BundleVerificationReason.BOUNDS_EXCEEDED
    assert opened == ["manifest.json", "payload.bin"]


def test_declared_artifact_over_the_total_bound_fails_before_child_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _publish(tmp_path, (("payload.bin", b"abcde"),))
    limits = BundleReadLimits(
        manifest_max_bytes=MANIFEST_MAX_BYTES,
        manifest_max_depth=64,
        max_artifacts=1,
        max_bytes_per_artifact=100,
        max_total_artifact_bytes=3,
    )
    opened = _record_child_opens(monkeypatch)
    with pytest.raises(BundleVerificationError) as caught:
        _reader(publication.path, limits=limits).consume_and_verify_artifact(
            "payload.bin",
            _consume_nothing,
        )
    assert caught.value.reason is BundleVerificationReason.BOUNDS_EXCEEDED
    assert opened == ["manifest.json"]


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
