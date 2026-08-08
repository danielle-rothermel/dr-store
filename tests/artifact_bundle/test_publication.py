from __future__ import annotations

import datetime as dt
import hashlib
import json
import stat
import threading
import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest
from dr_serialize import SerializationError

from dr_store import (
    ArtifactBundlePublication,
    BundleAllocationError,
    BundlePublicationPhase,
    BundlePublishError,
)
from dr_store.artifact_bundle import publication as publication_module

if TYPE_CHECKING:
    from pathlib import Path

    from dr_store import BundleArtifactWriter

WATCHDOG_SECONDS = 60
FROZEN_NOW = dt.datetime(2026, 8, 8, tzinfo=dt.UTC)


class _FrozenDatetime(dt.datetime):
    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW


def _allocate(root: Path) -> ArtifactBundlePublication:
    return ArtifactBundlePublication.allocate(root, prefix="run")


def _read_manifest(
    publication: ArtifactBundlePublication,
) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        json.loads((publication.path / "manifest.json").read_bytes()),
    )


def test_allocate_creates_one_fresh_prefixed_directory(tmp_path: Path) -> None:
    publication = _allocate(tmp_path)

    assert publication.path.is_dir()
    assert publication.path.parent == tmp_path
    assert publication.path.name.startswith("run-")
    assert list(publication.path.iterdir()) == []


@pytest.mark.parametrize(
    "prefix",
    ["", ".", "..", "/absolute", "nested/name", "nested\\name", "x\0y"],
)
def test_allocate_rejects_unsafe_prefix_before_filesystem_work(
    tmp_path: Path,
    prefix: str,
) -> None:
    with pytest.raises(BundleAllocationError):
        ArtifactBundlePublication.allocate(tmp_path, prefix=prefix)
    assert list(tmp_path.iterdir()) == []


def test_allocate_failure_is_typed_and_preserves_os_cause(
    tmp_path: Path,
) -> None:
    with pytest.raises(BundleAllocationError) as caught:
        _allocate(tmp_path / "missing")
    assert isinstance(caught.value.__cause__, OSError)


def test_generated_allocation_collision_is_visible_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_uuid = uuid.UUID(int=0)
    monkeypatch.setattr(publication_module.uuid, "uuid4", lambda: fixed_uuid)
    monkeypatch.setattr(publication_module.dt, "datetime", _FrozenDatetime)
    first = _allocate(tmp_path)

    with pytest.raises(BundleAllocationError) as caught:
        _allocate(tmp_path)

    assert isinstance(caught.value.__cause__, FileExistsError)
    assert [path.name for path in tmp_path.iterdir()] == [first.path.name]


def test_rejected_artifact_name_does_not_poison_publication(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)

    with pytest.raises(BundleAllocationError):
        publication.open_artifact("../escape")

    publication.publish(None)
    assert (publication.path / "manifest.json").is_file()


def test_writer_streams_exact_bytes_and_finalizes_descriptor(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)
    writer = publication.open_artifact("stdout.bin")
    chunks = [b"first", b"", b"-second", bytes(range(32))]
    for chunk in chunks:
        writer.write(chunk)

    descriptor = writer.finalize()
    stored = b"".join(chunks)
    artifact = publication.path / "stdout.bin"
    assert artifact.read_bytes() == stored
    assert stat.S_ISREG(artifact.stat().st_mode)
    assert descriptor.name == "stdout.bin"
    assert descriptor.sha256 == hashlib.sha256(stored).hexdigest()
    assert descriptor.byte_length == len(stored)


def test_publish_orders_descriptors_and_writes_pinned_manifest(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)
    for name, content in (("z.bin", b"last"), ("a.bin", b"first")):
        writer = publication.open_artifact(name)
        writer.write(content)
        writer.finalize()

    publication.publish({"kind": "example", "version": 1})

    manifest = _read_manifest(publication)
    assert isinstance(manifest, dict)
    assert manifest == {
        "format": "dr-store-artifact-bundle-v1",
        "artifacts": [
            {
                "name": "a.bin",
                "sha256": hashlib.sha256(b"first").hexdigest(),
                "byte_length": 5,
            },
            {
                "name": "z.bin",
                "sha256": hashlib.sha256(b"last").hexdigest(),
                "byte_length": 4,
            },
        ],
        "payload": {"kind": "example", "version": 1},
    }
    assert {path.name for path in publication.path.iterdir()} == {
        "a.bin",
        "z.bin",
        "manifest.json",
    }


def test_duplicate_rejection_before_admission_does_not_poison(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)
    writer = publication.open_artifact("stdout.bin")

    with pytest.raises(BundleAllocationError):
        publication.open_artifact("stdout.bin")

    writer.write(b"complete")
    writer.finalize()
    publication.publish(None)
    assert (publication.path / "manifest.json").is_file()


def test_active_writer_refusal_is_nonterminal_and_touches_no_manifest(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)
    writer = publication.open_artifact("stdout.bin")

    with pytest.raises(BundlePublishError) as caught:
        publication.publish({"state": "early"})
    assert caught.value.phase is BundlePublicationPhase.PRECONDITION
    assert caught.value.replacement_state is None
    assert not (publication.path / "manifest.json").exists()
    assert all(
        not path.name.startswith(".dr-store-artifact-bundle-")
        for path in publication.path.iterdir()
    )

    writer.finalize()
    publication.publish({"state": "complete"})
    assert _read_manifest(publication)["payload"] == {"state": "complete"}


def test_invalid_payload_refusal_is_nonterminal_and_preserves_cause(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)

    with pytest.raises(BundlePublishError) as caught:
        publication.publish({"invalid": float("inf")})

    assert caught.value.phase is BundlePublicationPhase.PRECONDITION
    assert caught.value.replacement_state is None
    assert isinstance(caught.value.__cause__, SerializationError)
    assert list(publication.path.iterdir()) == []

    publication.publish({"valid": True})
    assert _read_manifest(publication)["payload"] == {"valid": True}


def test_successful_publish_is_terminal_for_every_mutation(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)
    publication.publish(None)
    original = (publication.path / "manifest.json").read_bytes()

    with pytest.raises(BundlePublishError) as republish:
        publication.publish({"version": 2})
    assert republish.value.phase is BundlePublicationPhase.PRECONDITION
    assert republish.value.replacement_state is None
    with pytest.raises(BundleAllocationError):
        publication.open_artifact("late.bin")
    assert (publication.path / "manifest.json").read_bytes() == original


def test_concurrent_distinct_writers_cannot_publish_early(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)
    writers = {
        name: publication.open_artifact(name)
        for name in ("stdout.bin", "stderr.bin")
    }
    ready = threading.Barrier(3)
    release = threading.Event()
    finished = {name: threading.Event() for name in writers}
    failures: list[BaseException] = []

    def write(name: str, writer: BundleArtifactWriter) -> None:
        try:
            ready.wait(timeout=WATCHDOG_SECONDS)
            assert release.wait(WATCHDOG_SECONDS), (
                "writer release gate was not opened"
            )
            writer.write(name.encode())
            writer.finalize()
            finished[name].set()
        except BaseException as exc:  # noqa: BLE001 - returned to test owner
            failures.append(exc)

    threads = [
        threading.Thread(target=write, args=(name, writer))
        for name, writer in writers.items()
    ]
    for thread in threads:
        thread.start()
    try:
        ready.wait(timeout=WATCHDOG_SECONDS)
        with pytest.raises(BundlePublishError) as caught:
            publication.publish({"state": "early"})
        assert caught.value.phase is BundlePublicationPhase.PRECONDITION
        assert not (publication.path / "manifest.json").exists()
        release.set()
        assert all(event.wait(WATCHDOG_SECONDS) for event in finished.values())
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=WATCHDOG_SECONDS)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    publication.publish({"state": "complete"})
    manifest = _read_manifest(publication)
    assert [item["name"] for item in manifest["artifacts"]] == [
        "stderr.bin",
        "stdout.bin",
    ]


def test_concurrent_exact_name_reservation_admits_one_writer(
    tmp_path: Path,
) -> None:
    publication = _allocate(tmp_path)
    start = threading.Barrier(3)
    writers: list[BundleArtifactWriter] = []
    failures: list[BaseException] = []

    def reserve() -> None:
        start.wait(timeout=WATCHDOG_SECONDS)
        try:
            writers.append(publication.open_artifact("shared.bin"))
        except BaseException as exc:  # noqa: BLE001 - returned to test owner
            failures.append(exc)

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=WATCHDOG_SECONDS)
    for thread in threads:
        thread.join(timeout=WATCHDOG_SECONDS)

    assert all(not thread.is_alive() for thread in threads)
    assert len(writers) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], BundleAllocationError)
    writers[0].finalize()
    publication.publish(None)
