from __future__ import annotations

import json
import selectors
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

from dr_store.document_file import CanonicalJsonFile
from dr_store.document_file.canonical_json import (
    _is_reserved_document_temp_name,
)

if TYPE_CHECKING:
    from pathlib import Path

DOCUMENT_NAME = "document.json"
MAX_BYTES = 1 << 20
WATCHDOG_SECONDS = 60
FIRST = {"version": 1}

_CHILD = f"""
import json
import sys
from dr_store.document_file import CanonicalJsonFile
from dr_store.document_file import canonical_json as file_module

document = CanonicalJsonFile(
    sys.argv[1],
    {DOCUMENT_NAME!r},
    max_bytes={MAX_BYTES!r},
)
document.publish({FIRST!r})
original_replace = file_module._replace

def stop_before_replace(source, target, *, directory_descriptor):
    event = {{"event": "before-replace", "temporary_name": source}}
    sys.stdout.write(json.dumps(event) + "\\n")
    sys.stdout.flush()
    sys.stdin.read()
    original_replace(
        source,
        target,
        directory_descriptor=directory_descriptor,
    )

file_module._replace = stop_before_replace
document.publish({{"version": 2}})
"""


def test_process_death_before_replace_preserves_target_and_owned_orphan(
    tmp_path: Path,
) -> None:
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _CHILD, str(tmp_path)],
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
                "child did not reach the pre-replacement gate"
            )
        line = process.stdout.readline()
        if not line:
            assert process.stderr is not None
            raise AssertionError(
                "child exited before the pre-replacement gate:\n"
                f"{process.stderr.read()}"
            )
        event: dict[str, object] = json.loads(line)
        assert event["event"] == "before-replace"
        temporary_name = str(event["temporary_name"])
        assert _is_reserved_document_temp_name(temporary_name)

        process.send_signal(signal.SIGKILL)
        assert process.wait(timeout=WATCHDOG_SECONDS) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=WATCHDOG_SECONDS)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    document = CanonicalJsonFile(
        tmp_path,
        DOCUMENT_NAME,
        max_bytes=MAX_BYTES,
    )
    assert document.read() == FIRST
    children = {path.name for path in tmp_path.iterdir()}
    assert children == {DOCUMENT_NAME, temporary_name}
