from __future__ import annotations

import json
import selectors
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from dr_store import DocumentDirectory

if TYPE_CHECKING:
    from dr_serialize import Jsonable

MANIFEST_NAME = "record.json"
MANIFEST_MAX_BYTES = 1 << 20
SIDECAR_NAME = "stdout.bin"
FIRST: Jsonable = {"state": "collecting", "sidecars": []}
WATCHDOG_SECONDS = 60

_CHILD = f"""
import json
import sys
from dr_store import DocumentDirectory

FIRST = {FIRST!r}
SIDECAR_NAME = {SIDECAR_NAME!r}

directory = DocumentDirectory.allocate(
    sys.argv[1],
    prefix="run",
    manifest_name={MANIFEST_NAME!r},
    manifest_max_bytes={MANIFEST_MAX_BYTES!r},
)
directory.publish(FIRST)
writer = directory.open_sidecar(SIDECAR_NAME, head_cap=100, tail_cap=60)
writer.write(b"sidecar-payload" * 64)

if sys.argv[2] == "unfinalized":
    event = {{"event": "ready", "path": str(directory.path)}}
elif sys.argv[2] == "published-sidecar":
    summary = writer.finalize()
    directory.publish({{
        "state": "complete",
        "sidecars": [{{
            "name": SIDECAR_NAME,
            "digest": summary.digest,
            "head_length": summary.head_length,
            "tail_length": summary.tail_length,
        }}],
    }})
    event = {{"event": "ready", "path": str(directory.path)}}
else:
    raise ValueError(f"unknown scenario: {{sys.argv[2]}}")

sys.stdout.write(json.dumps(event) + "\\n")
sys.stdout.flush()
sys.stdin.read()
"""


def _run_to_completion_then_kill(root: Path, scenario: str) -> Path:
    # Evidence begins after public operations return; it excludes mid-commit
    # interruption and power loss.
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _CHILD, str(root), scenario],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=WATCHDOG_SECONDS), (
                "child did not announce completed operations"
            )
        line = process.stdout.readline()
        if not line:
            assert process.stderr is not None
            raise AssertionError(
                "child exited before announcing completed operations:\n"
                f"{process.stderr.read()}"
            )
        event: dict[str, object] = json.loads(line)
        assert event["event"] == "ready"
        path = Path(str(event["path"]))

        process.send_signal(signal.SIGKILL)
        returncode = process.wait(timeout=WATCHDOG_SECONDS)
        assert returncode == -signal.SIGKILL
        return path
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=WATCHDOG_SECONDS)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _read(path: Path) -> Jsonable:
    return DocumentDirectory(
        path,
        MANIFEST_NAME,
        manifest_max_bytes=MANIFEST_MAX_BYTES,
    ).read_manifest()


def test_unfinalized_sidecar_does_not_damage_published_manifest_after_death(
    tmp_path: Path,
) -> None:
    path = _run_to_completion_then_kill(tmp_path, "unfinalized")

    assert _read(path) == FIRST


def test_published_sidecar_is_verifiable_from_manifest_after_death(
    tmp_path: Path,
) -> None:
    path = _run_to_completion_then_kill(tmp_path, "published-sidecar")

    manifest = _read(path)
    assert isinstance(manifest, dict)
    assert manifest["state"] == "complete"
    sidecars = manifest["sidecars"]
    assert isinstance(sidecars, list)
    assert len(sidecars) == 1
    sidecar = sidecars[0]
    assert isinstance(sidecar, dict)
    name = sidecar["name"]
    digest = sidecar["digest"]
    head_length = sidecar["head_length"]
    tail_length = sidecar["tail_length"]
    assert isinstance(name, str)
    assert isinstance(digest, str)
    assert type(head_length) is int
    assert type(tail_length) is int

    directory = DocumentDirectory(
        path,
        MANIFEST_NAME,
        manifest_max_bytes=MANIFEST_MAX_BYTES,
    )
    directory.verify_sidecar(
        name,
        expected_digest=digest,
        expected_head_length=head_length,
        expected_tail_length=tail_length,
    )
