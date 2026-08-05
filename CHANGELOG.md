# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- `DocumentDirectory.verify_sidecar` accepts a safe direct-child Sidecar name
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

- Document Directory: `DocumentDirectory` allocates one durable directory
  per document (`<prefix>-<utc-timestamp>-<uuid4>`, created with
  `exist_ok=False` so a collision is typed rather than retried), publishes
  one atomically-replaced canonical-JSON Manifest, and opens streamed
  binary Sidecars beside it. Prefixes, Manifest names, and Sidecar names
  are validated safe single path segments.
- Atomic durable publish: every `publish()` writes the complete canonical
  JSON to a temp file in the same directory, flushes it with `F_FULLFSYNC`
  where available and `os.fsync` otherwise, atomically renames it onto the
  Manifest name, and flushes the directory entry — so after abrupt process
  death a reader sees either no Manifest or one complete previously
  published Manifest. Scoped to local macOS filesystems.
- `SidecarWriter` owning truncation mechanics — `head_cap` bytes fill
  first, a ring buffer keeps the last `tail_cap` bytes of the remainder,
  and the file stores head segment then tail segment — plus the frozen
  `SidecarSummary` reporting stored segment lengths, `produced`,
  `dropped`, and the Sidecar Digest: the full 64-character lowercase
  SHA-256 of the stored bytes, which is not a Content Hash.
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
  durable under concurrent cross-process use, with `PutOutcome` and
  `BindOutcome` as the backend-level compare-and-set results.
- Typed error taxonomy rooted at `StoreError`: `ReferenceValidationError`,
  `ObjectConflictError`, `ObjectNotFoundError`, `SchemaMismatchError`,
  `ContentHashMismatchError`, and `BindingConflictError`.
- Vocabulary sheet defining the object storage contract, published at
  <https://danielle-rothermel.github.io/dr-store/>.
