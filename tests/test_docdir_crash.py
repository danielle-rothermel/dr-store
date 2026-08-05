"""Crash consistency: SIGKILL at each commit point, no partial Manifest.

A child process is driven step by step over a pipe. It performs one commit
point per command and announces completion with an explicit event line; the
parent reads that event, then delivers ``SIGKILL``. Every synchronization
point here is an event or a terminal outcome -- the parent never waits on
elapsed time, and no assertion treats the passage of time as evidence.

After every kill the surviving directory must show either no Manifest or
one complete previously-published Manifest, and every finalized Sidecar
must still verify against the summary the child reported.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from dr_store import DocumentDirectory, ManifestReadError

if TYPE_CHECKING:
    from collections.abc import Iterator

MANIFEST_NAME = "record.json"
SIDECAR_NAME = "stdout.bin"
FIRST = {"state": "first"}
SECOND = {"state": "second", "note": "x" * 4096}
WATCHDOG_SECONDS = 60

# Driven over stdin: one command per line, one event line per completed
# commit point. The child blocks on the next command, so it is only ever
# killed in a state the parent has already observed.
_CHILD = f"""
import json, sys
from dr_store import DocumentDirectory

FIRST = {FIRST!r}
SECOND = {SECOND!r}


def announce(event, **fields):
    sys.stdout.write(json.dumps({{"event": event, **fields}}) + "\\n")
    sys.stdout.flush()


directory = None
writer = None
for line in sys.stdin:
    command = line.strip()
    if command == "allocate":
        directory = DocumentDirectory.allocate(
            sys.argv[1], prefix="run", manifest_name={MANIFEST_NAME!r}
        )
        announce("allocated", path=str(directory.path))
    elif command == "publish-first":
        directory.publish(FIRST)
        announce("published", which="first")
    elif command == "publish-second":
        directory.publish(SECOND)
        announce("published", which="second")
    elif command == "open-sidecar":
        writer = directory.open_sidecar({SIDECAR_NAME!r})
        announce("sidecar-open")
    elif command == "write-sidecar":
        writer.write(b"sidecar-payload" * 64)
        announce("sidecar-written")
    elif command == "finalize-sidecar":
        summary = writer.finalize()
        announce(
            "sidecar-finalized",
            digest=summary.digest,
            head_length=summary.head_length,
            tail_length=summary.tail_length,
        )
    else:
        announce("unknown", command=command)
"""


class _Child:
    """A driven child process: send a command, read its completion event."""

    def __init__(self, root: Path) -> None:
        self._process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", _CHILD, str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def step(self, command: str) -> dict[str, object]:
        """Run one commit point and return the event it announced."""
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        self._process.stdin.write(f"{command}\n")
        self._process.stdin.flush()
        line = self._process.stdout.readline()
        assert line, f"child died before completing {command!r}"
        event: dict[str, object] = json.loads(line)
        return event

    @property
    def is_running(self) -> bool:
        """Whether the child has not yet terminated."""
        return self._process.poll() is None

    def kill_now(self) -> None:
        """Deliver SIGKILL and wait for the terminal outcome."""
        self._process.send_signal(signal.SIGKILL)
        # A watchdog only: the wait succeeds on process death, never on time.
        returncode = self._process.wait(timeout=WATCHDOG_SECONDS)
        assert returncode == -signal.SIGKILL
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None:
                stream.close()


@pytest.fixture
def child(tmp_path: Path) -> Iterator[_Child]:
    running = _Child(tmp_path)
    yield running
    if running.is_running:
        running.kill_now()


def _manifest_or_none(path: Path) -> object | None:
    try:
        return DocumentDirectory.read_manifest(
            path,
            manifest_name=MANIFEST_NAME,
        )
    except ManifestReadError:
        assert not (path / MANIFEST_NAME).exists(), (
            "a manifest survived the kill but is not a complete document"
        )
        return None


def test_kill_after_allocation_leaves_no_manifest(child: _Child) -> None:
    allocated = child.step("allocate")
    child.kill_now()
    path = Path(str(allocated["path"]))
    assert path.is_dir()
    assert _manifest_or_none(path) is None


def test_kill_after_first_publish_keeps_that_manifest(
    child: _Child,
) -> None:
    allocated = child.step("allocate")
    child.step("publish-first")
    child.kill_now()
    path = Path(str(allocated["path"]))
    assert _manifest_or_none(path) == FIRST
    assert [p.name for p in path.iterdir()] == [MANIFEST_NAME]


def test_kill_after_republish_keeps_the_second_manifest(
    child: _Child,
) -> None:
    allocated = child.step("allocate")
    child.step("publish-first")
    child.step("publish-second")
    child.kill_now()
    path = Path(str(allocated["path"]))
    assert _manifest_or_none(path) == SECOND


def test_kill_mid_sidecar_keeps_the_published_manifest(
    child: _Child,
) -> None:
    # The sidecar is open and written but never finalized: the manifest the
    # caller published before it must survive intact and unchanged.
    allocated = child.step("allocate")
    child.step("publish-first")
    child.step("open-sidecar")
    child.step("write-sidecar")
    child.kill_now()
    path = Path(str(allocated["path"]))
    assert _manifest_or_none(path) == FIRST


def test_kill_after_finalize_keeps_a_verifiable_sidecar(
    child: _Child,
) -> None:
    allocated = child.step("allocate")
    child.step("publish-first")
    child.step("open-sidecar")
    child.step("write-sidecar")
    finalized = child.step("finalize-sidecar")
    child.kill_now()
    path = Path(str(allocated["path"]))
    DocumentDirectory.verify_sidecar(
        path / SIDECAR_NAME,
        expected_digest=str(finalized["digest"]),
        expected_head_length=int(str(finalized["head_length"])),
        expected_tail_length=int(str(finalized["tail_length"])),
    )
    assert _manifest_or_none(path) == FIRST


def test_kill_after_final_publish_embeds_the_verified_sidecar(
    child: _Child,
) -> None:
    # The full ordering: publish, stream, finalize, publish again. After the
    # kill the durable manifest is the second one and the sidecar it
    # describes still verifies.
    allocated = child.step("allocate")
    child.step("publish-first")
    child.step("open-sidecar")
    child.step("write-sidecar")
    finalized = child.step("finalize-sidecar")
    child.step("publish-second")
    child.kill_now()
    path = Path(str(allocated["path"]))
    assert _manifest_or_none(path) == SECOND
    DocumentDirectory.verify_sidecar(
        path / SIDECAR_NAME,
        expected_digest=str(finalized["digest"]),
        expected_head_length=int(str(finalized["head_length"])),
        expected_tail_length=int(str(finalized["tail_length"])),
    )


def test_reader_never_observes_a_partial_manifest_across_republish(
    child: _Child,
) -> None:
    # This process reads the manifest after each of the child's publishes
    # has announced completion, so every observation falls in a quiescent
    # window: what it pins is that a committed publish leaves one complete
    # document behind across repeated replacement, not that a reader racing
    # a writer sees no prefix. That race is pinned in
    # tests/test_docdir_manifest.py.
    allocated = child.step("allocate")
    path = Path(str(allocated["path"]))
    child.step("publish-first")
    observed = []
    for _ in range(16):
        child.step("publish-second")
        observed.append(_manifest_or_none(path))
        child.step("publish-first")
        observed.append(_manifest_or_none(path))
    child.kill_now()
    assert all(seen in (FIRST, SECOND) for seen in observed)
    assert [p.name for p in path.iterdir()] == [MANIFEST_NAME]
