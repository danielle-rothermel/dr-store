# Document Directory component

dr-store's second component: a durable, crash-consistent directory holding
one atomic canonical-JSON **Manifest** plus streamed binary **Sidecars**.
Domain-neutral; first consumer is dr-exec's run store. Additive: the
Object Store contract, backends, and errors are untouched.

## Vocabulary

- **Document Directory** — one allocated directory, one writer, one
  Manifest, zero or more Sidecars.
- **Manifest** — the single atomically-replaced canonical-JSON document;
  the source of truth about every Sidecar (meaning, lengths, digest).
- **Sidecar** — a raw-bytes artifact written incrementally beside the
  Manifest; possibly truncated; never self-describing.
- **Sidecar Digest** — SHA-256 over a Sidecar's stored bytes. Not a
  Content Hash (raw bytes, not a canonical record).
- No term overlaps Object Store vocabulary ("object", "record", "Content
  Hash", "binding"). The vocab sheet gains a second section; the
  append-only contract's wording is not edited.

## Placement

- Module `dr_store.docdir`, exported from the package root.
- Same distribution and version as the Object Store.
- Dependency unchanged: dr-serialize only.

## Neutrality boundaries

The component never knows:

- **Lifecycle**: no state names, no transition legality. `publish()` is
  last-write-wins; callers own state machines via their own typed handles.
- **Manifest schema**: the payload is an opaque `Jsonable`; the component
  never reads fields out of it.
- **Retention policy**: byte caps arrive as parameters; computing them is
  caller domain.
- **Processes**: push-style writer API only; the component never drains
  fds, owns threads, or manages child processes.

Concurrency claim: concurrent allocation under one root is collision-free;
each allocated directory has exactly one writer by construction, not by
locking. No cross-process coordination is claimed.

## Surface

```python
from dr_store.docdir import DocumentDirectory, SidecarWriter, SidecarSummary

d = DocumentDirectory.allocate(root, prefix="run", manifest_name="record.json")
d.publish(manifest)                                  # Jsonable -> atomic durable replace
w = d.open_sidecar("stdout.bin", head_cap=..., tail_cap=...)
w.write(chunk)
summary = w.finalize()                               # -> SidecarSummary
d.publish(final_manifest)                            # caller embeds summaries first

DocumentDirectory.read_manifest(path, manifest_name="record.json")   # -> Jsonable
DocumentDirectory.verify_sidecar(
    path,
    expected_digest=...,
    expected_head_length=...,
    expected_tail_length=...,
)
```

- `allocate()` creates `<prefix>-<utc-timestamp>-<uuid4>` under `root` with
  `mkdir(exist_ok=False)`; a collision is a typed error, never a retry
  loop.
- `prefix`, `manifest_name`, and sidecar names are validated single-segment
  safe names (no separators, no `.`/`..`).
- `SidecarSummary` is a frozen slotted dataclass: `head_length`,
  `tail_length`, `produced`, `dropped`, `digest`. It is never serialized by
  dr-store; callers project it into their own models.
- Finalization ordering is structural: a summary exists only after
  `finalize()`, so a manifest embedding digests cannot precede sidecar
  flush.

## Truncation

The writer owns the mechanics; callers own only the cap values.

- `head_cap` bytes fill first; a ring buffer keeps the last `tail_cap`
  bytes of the remainder.
- Stored file layout: head segment then tail segment, one file.
- Unbounded: no caps. Head-only: `tail_cap=0`.
- The summary reports `produced` (total bytes offered) and `dropped`
  alongside the stored segment lengths.

## Verified read

- `read_manifest()` verifies strict JSON and canonical form.
- `verify_sidecar()` checks stored bytes against caller-supplied
  expectations (digest, segment lengths). The component stays
  schema-blind: callers extract expectations from their own manifest.

## Durability

Every `publish()`:

1. write the complete canonical manifest to a temp file in the same
   directory;
2. flush: `flush()` + `F_FULLFSYNC` where available, `os.fsync` fallback;
3. `os.replace()` onto `manifest_name` (same-filesystem atomic rename);
4. flush the directory fd (same `F_FULLFSYNC`-then-`fsync` ladder).

`SidecarWriter.finalize()` flushes the sidecar file the same way before
returning its summary.

Claim: after abrupt process death, a reader sees either no manifest or one
complete previously-published manifest, never a partial one. Qualified
scope: local macOS filesystems; network mounts and cloud-synchronized
directories are outside the claim.

## Canonicalization and digests

- Manifest bytes on disk are
  `canonical_json(validate_strict_json(payload))` — the one dr-serialize
  dialect; no second canonicalization.
- Sidecar Digest: full 64-char lowercase SHA-256 hex over the stored file
  bytes (post-truncation), computed incrementally and finalized in
  `finalize()`.

## Errors

- Base `DocumentDirectoryError`; concrete: `AllocationError`,
  `ManifestPublishError`, `ManifestReadError` (missing, malformed,
  non-strict, or non-canonical), `SidecarVerificationError` (length or
  digest mismatch).
- Original OS and decoding exceptions are preserved as `__cause__`.

## Qualification

- Crash consistency at each commit point: subprocess kill after explicit
  committed-state events; synchronize on store events, never sleeps or
  elapsed time.
- Atomic-replace goldens; a reader never observes a partial manifest.
- Truncation edges: exact head/tail recovery, `produced`/`dropped` counts,
  unbounded and head-only cases.
- Digest verification: matching sidecars load; mutated bytes and mismatched
  lengths fail typed.
- Concurrent distinct-directory allocation is collision-free.
- Safe-name rejection for prefixes, manifest names, and sidecar names.
