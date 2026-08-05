# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- `DocumentDirectory.verify_sidecar` accepts a lexically valid direct-child
  Sidecar name
  on a directory instance. Verification opens the Document Directory as its
  authority, refuses final-component symlinks for both that directory and its
  named child, requires a regular child file, and streams bounded reads from
  the exact descriptor it inspected. Platforms without directory-relative
  no-follow opens fail closed.
- Reorganized the implementation around the top-level functional areas
  `content_addressing`, `object_store`, `storage_backends`, and
  `document_directory`, with supporting errors and filesystem mechanics under
  `core`. The flat `dr_store` exports and their behavior are unchanged.
  Internal module paths are a hard cutover with no compatibility aliases;
  existing pickle payloads tied to the previous defining modules are not
  compatible with this layout.

## [0.1.1] - 2026-08-05

### Added

- Document Directory: `DocumentDirectory` allocates one directory
  per document (`<prefix>-<utc-timestamp>-<uuid4>`, created with
  `exist_ok=False` so a collision is typed rather than retried), publishes
  one canonical-JSON Manifest by same-directory replacement, and opens streamed
  binary Sidecars beside it. Prefix, Manifest, and Sidecar name validation
  prevents lexical traversal syntax only; Manifest reads and Sidecar creation
  and writes follow existing final-component symlinks and require trusted
  directory contents. Allocation does not flush the caller-owned root directory.
- Manifest publish: every `publish()` writes the complete canonical JSON to a
  temp file in the same directory, flushes it, replaces the Manifest, and
  flushes the directory entry. All-or-nothing visibility depends on the
  underlying filesystem honoring atomic same-directory replacement; network,
  synchronized, or other filesystems whose rename semantics are not established
  are outside current evidence. A final directory-flush failure raises even
  though the replacement may already be visible, and publication does not
  promise power-loss durability.
- `SidecarWriter` owning truncation mechanics — `head_cap` bytes fill
  first, a ring buffer keeps the last `tail_cap` bytes of the remainder,
  and the file stores head segment then tail segment — plus the frozen
  `SidecarSummary` reporting stored segment lengths, `produced`,
  `dropped`, and the Sidecar Digest: the full 64-character lowercase
  SHA-256 of the stored bytes, which is not a Content Hash. Finalization
  flushes the Sidecar descriptor before returning the summary, but does not
  flush the containing directory entry.
- Verified read paths `DocumentDirectory.read_manifest` (strict *and*
  canonical JSON) and `DocumentDirectory.verify_sidecar` (caller-supplied
  digest and total segment length), keeping the component schema-blind.
- Typed error taxonomy rooted at `DocumentDirectoryError`, independent of
  `StoreError`: `AllocationError`, `ManifestPublishError`,
  `ManifestReadError`, and `SidecarVerificationError`, each preserving the
  originating OS or decoding exception as `__cause__`.
- Vocabulary sheet section defining the Document Directory contract:
  Document Directory, Manifest, Sidecar, and Sidecar Digest.

## [0.1.0] - 2026-07-24

Initial release.

### Added

- Content addressing: `compute_content_hash`, `is_content_hash`, and
  `CONTENT_HASH_LENGTH` — the full 64-character lowercase SHA-256 Content
  Hash of a complete canonical record, computed through `dr-serialize`'s
  canonical JSON with no second canonicalization dialect.
- Typed content-addressed handle `ObjectReference`, validated at
  construction so an empty schema or malformed content hash can never
  enter the store.
- `ObjectStore` owning three operations and nothing else: immutable put
  (`PutStatus`), verified get, and one generic atomic key-to-reference
  binding (`BindStatus`) — every write append-only, replay idempotent,
  and differing content a typed conflict that never overwrites.
- Two interchangeable backends behind the neutral `Backend` protocol:
  `MemoryBackend` for tests and single-process use, and `SqliteBackend`
  persisting data under concurrent cross-process use, with `PutOutcome` and
  `BindOutcome` as the backend-level compare-and-set results.
- Typed error taxonomy rooted at `StoreError`: `ReferenceValidationError`,
  `ObjectConflictError`, `ObjectNotFoundError`, `SchemaMismatchError`,
  `ContentHashMismatchError`, and `BindingConflictError`.
- Vocabulary sheet defining the object storage contract, published at
  <https://danielle-rothermel.github.io/dr-store/>.
