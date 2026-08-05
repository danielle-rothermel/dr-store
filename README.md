# dr-store

Generic durable storage primitives for the dr-* stack: an append-only
content-addressed object store and a crash-consistent document directory.

## Object Store

The Object Store owns three things and nothing else:

1. **Immutable put** — an absent typed `(schema, content_hash)` key
   atomically accepts a verified complete canonical record value; replay of
   the same canonical value is idempotent success; different content at the
   same key is a typed conflict and never overwrites the stored value.
2. **Verified get** — every read recomputes and verifies the Content Hash
   and schema declared by the `ObjectReference`; missing, schema-mismatched,
   or corrupted content fails with a typed error.
3. **Atomic key-to-reference binding** — one generic compare-and-set that
   binds an opaque caller-owned key to an `ObjectReference`: an unbound key
   binds; the same reference replays idempotently; a different reference
   conflicts and never overwrites the winner. No overwrite path is exposed.

The Content Hash is the full 64-character lowercase SHA-256 digest of the
complete canonical persisted record, canonicalized through
[`dr-serialize`](https://github.com/danielle-rothermel/dr-serialize)'s
canonical JSON. `dr-store` does not invent a second canonicalization
dialect, and a Content Hash is not an Identity Hash.

## Document Directory

The Document Directory stores what a single immutable record cannot: one
allocated directory with exactly one writer, one atomically-replaced
canonical-JSON **Manifest**, and zero or more streamed binary **Sidecars**.

```python
from dr_store import DocumentDirectory

directory = DocumentDirectory.allocate(
    root, prefix="run", manifest_name="record.json"
)
directory.publish(manifest)                    # atomic durable replace
writer = directory.open_sidecar("stdout.bin", head_cap=..., tail_cap=...)
writer.write(chunk)
summary = writer.finalize()                    # -> SidecarSummary
directory.publish(final_manifest)              # summaries embedded by caller
```

- **Atomic durable publish** — each `publish()` writes the complete
  canonical JSON to a temp file in the same directory, flushes it
  (`F_FULLFSYNC` where available, `os.fsync` otherwise), renames it onto
  the manifest name, and flushes the directory entry. After abrupt process
  death a reader sees either no Manifest or one complete previously
  published Manifest — never a partial one. The claim is scoped to local
  macOS filesystems; network mounts and cloud-synchronized directories are
  outside it.
- **Writer-owned truncation** — `head_cap` bytes fill first and a ring
  buffer keeps the last `tail_cap` bytes of the remainder, stored as head
  segment then tail segment in one file. The `SidecarSummary` reports the
  stored segment lengths alongside `produced` and `dropped` byte counts,
  plus the Sidecar Digest: the full 64-character lowercase SHA-256 of the
  stored bytes. A Sidecar Digest is not a Content Hash — its input is raw
  bytes, not a canonical record.
- **Verified read-back** — `read_manifest()` accepts only strict canonical
  JSON; `verify_sidecar()` checks stored bytes against the digest and
  segment lengths the caller extracted from its own Manifest. Every fault
  is a typed error under `DocumentDirectoryError`.

The component is domain-neutral in the same way the Object Store is, and
narrower still: it knows no lifecycle state, never reads a field out of a
Manifest payload, never computes a retention policy, and never owns
threads or child processes. Concurrent allocation under one root is
collision-free; each allocated directory has one writer by construction,
not by locking.

The [vocabulary sheet](https://danielle-rothermel.github.io/dr-store/)
(source: `.defs/vocab.html`) is the authoritative statement of both
storage contracts this repo implements: the terms, the guarantees, what is
in and out of scope, and the mapping from each term to the exported names.

## Ecosystem

`dr-store` depends only on `dr-serialize` for canonical JSON and strict
finite-JSON validation. It carries no Whetstone, Rollout, workflow, retry,
or campaign vocabulary; the public contract is domain-neutral.

## Object Store backends

- **in-memory** (`MemoryBackend`) — for tests and single-process use.
- **sqlite** (`SqliteBackend`) — durable and safe under concurrent
  cross-process use via serialized transactions.

Both satisfy the same backend-neutral contract, exercised by a shared
concurrency test proving parallel binds of one unbound key produce exactly
one winner.
