# dr-store

[![CI](https://github.com/danielle-rothermel/dr-store/actions/workflows/ci.yml/badge.svg)](https://github.com/danielle-rothermel/dr-store/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dr-store.svg)](https://pypi.org/project/dr-store/)

[Definitions](https://danielle-rothermel.github.io/dr-store/) ·
[Changelog](CHANGELOG.md) ·
[dr-serialize](https://github.com/danielle-rothermel/dr-serialize)

dr-store provides domain-neutral storage primitives for immutable records and
document artifacts:

- **[Content addressing](src/dr_store/content_addressing.py)**
  identifies complete records by their declared schemas and SHA-256 hashes of
  their Canonical JSON Text under dr-serialize's frozen profile.
- **[Object Store](src/dr_store/object_store.py)**
  provides immutable puts, verified reads, and atomic bindings from opaque
  caller-owned keys to object references.
- **[Storage backends](src/dr_store/storage_backends)**
  supply the Object Store's atomic, append-only operations. `MemoryBackend` is
  process-local; `SqliteBackend` persists data for cross-process use.
- **[Document Directory](src/dr_store/document_directory)**
  publishes one canonical Manifest beside streamed binary Sidecars.
- **[Core infrastructure](src/dr_store/core)**
  provides typed failures, single-segment name validation, and filesystem flush
  helpers.

## Usage

```python
from dr_store import MemoryBackend, ObjectStore

store = ObjectStore(MemoryBackend())
reference, _ = store.put("example.note.v1", {"title": "hello"})
store.bind("notes/latest", reference)

assert store.resolve("notes/latest") == reference
assert store.get(reference) == {"title": "hello"}
```

Use `SqliteBackend(path)` when the stored objects and bindings must persist
across processes. The [definitions](https://danielle-rothermel.github.io/dr-store/)
describe the full vocabulary and map it to the public exports.

## Filesystem and failure semantics

A Document Directory is intended for caller-coordinated single-writer use;
dr-store does not enforce that policy with a lock. Allocation uses a timestamp
and UUID4, but a generated-name collision raises `AllocationError` rather than
being retried. Allocation does not flush the caller-owned root directory.

Manifest publication writes and flushes a temporary file, replaces the Manifest
in the same directory, and then flushes the Document Directory. All-or-nothing
visibility depends on the underlying filesystem honoring atomic same-directory
replacement; network, synchronized, or other filesystems whose rename semantics
are not established are outside current evidence. A failure of the final flush
raises even though the replacement may already be visible, and it does not roll
the Manifest back. These operations do not promise power-loss durability.

Name validation prevents lexical traversal syntax only. Manifest reads and
Sidecar creation and writes follow existing final-component symlinks, so those
paths require trusted, caller-controlled directory contents. Sidecar
finalization flushes the Sidecar descriptor before returning its summary, but it
does not flush the Sidecar's directory entry or impose ordering on an arbitrary
Manifest publication. Sidecar verification is the no-follow path: it refuses
final-component symlinks for both the Document Directory and named child,
requires a regular direct child, and reads from the descriptor it inspected.

A failed Sidecar `write` raises `AllocationError` but does not close or poison
the writer, and accounting may already include the failed chunk. Callers must
abandon that writer; retrying it or finalizing it has no supported outcome.
