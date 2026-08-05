# dr-store

Domain-neutral storage primitives for immutable records and durable document
artifacts.

## At a Glance

- **Documentation:** [Storage contracts and vocabulary](https://danielle-rothermel.github.io/dr-store/)
- **Package version:** `0.1.1`
- **Python:** 3.12 or later
- **Personally owned dependencies:**
  - [dr-serialize](https://github.com/danielle-rothermel/dr-serialize) — current
    release `0.1.1`; dr-store requires `>=0.1.0`

## High-Level Design

dr-store provides two complementary storage capabilities with shared strict
JSON handling and typed failures:

- **Object references and content hashing** identify a complete record by its
  declared schema and the SHA-256 digest of its canonical JSON representation.
  References validate their own shape, and stored records are reverified when
  read.
- **The Object Store** stores immutable records and optionally binds opaque,
  caller-owned keys to their references. Repeating the same write is
  idempotent; conflicting writes preserve the value that was stored first.
- **Storage backends** supply the atomic persistence operations used by the
  Object Store. The library includes an in-memory implementation and a SQLite
  implementation with the same observable storage behavior.
- **The Document Directory** manages one atomically published canonical-JSON
  manifest alongside streamed binary sidecars. Sidecars can retain a bounded
  head and tail of a stream, report what was kept or dropped, and be verified
  after writing.
- **Typed errors** distinguish invalid references, missing or corrupted
  content, conflicting writes, publication failures, and sidecar verification
  failures.

Canonical JSON validation, rendering, and hashing come from `dr-serialize`, so
the library uses one serialization dialect across references, stored records,
and manifests. Domain schemas, lifecycle rules, retention policy, and the
meaning of binding keys remain the caller's responsibility.

## Object Store

The Object Store is an append-only, content-addressed store for complete
JSON-compatible records. It supports:

- immutable puts addressed by schema and content hash;
- verified reads that detect missing, mismatched, or corrupted content;
- atomic key-to-reference bindings with no overwrite, clear, or rebind path;
- idempotent replay of an identical record or binding; and
- typed conflicts that preserve the existing durable value.

```python
from dr_store import MemoryBackend, ObjectStore

store = ObjectStore(MemoryBackend())
reference, put_status = store.put("example.record", {"value": 42})
record = store.get(reference)

bind_status = store.bind("latest", reference)
assert store.resolve("latest") == reference
```

## Document Directory

A Document Directory holds one canonical-JSON manifest and zero or more binary
sidecars in an allocated directory. Manifest publication uses an atomic durable
replace, while sidecar writers support unbounded output, head-only retention,
or bounded head-and-tail retention.

```python
from dr_store import DocumentDirectory

directory = DocumentDirectory.allocate(
    root,
    prefix="run",
    manifest_name="record.json",
)
directory.publish(initial_manifest)

writer = directory.open_sidecar(
    "stdout.bin",
    head_cap=64_000,
    tail_cap=64_000,
)
writer.write(chunk)
summary = writer.finalize()

directory.publish(final_manifest)
```

The manifest is opaque to dr-store and remains the caller's source of truth
about its sidecars. The library verifies manifest canonicality and can verify a
sidecar's stored length and digest, but it does not interpret manifest fields or
own application lifecycle state.

## Backends

- `MemoryBackend` is intended for tests and single-process use.
- `SqliteBackend` provides durable storage and serializes concurrent writes
  across processes.

Both backends implement the same append-only object and binding operations, so
callers choose a backend without changing Object Store semantics.
