# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.3] - 2026-08-12

### Changed

- Toolchain refreshed to current releases: ruff 0.16, ty 0.0.70, pre-commit
  4.6.2, tombi 1.3.2; runtime floors raised to the tested minimums
  (pydantic 2.13, psycopg 3.3). Runtime resolutions were already latest.

### Added

- Added `ObjectStore.evict_bindings(keys)` and the `EvictStatus` enum
  (`EVICTED`, `ABSENT`), the cache-grade operation that makes memoized
  bindings bustable. It deletes binding rows for the given exact keys and
  never touches object rows, so evicted content stays retrievable by reference
  and other keys bound to it keep resolving. Absent keys report `ABSENT`
  instead of raising, so replay is idempotent. There is no prefix form and,
  deliberately, no enlisted variant: eviction is structurally unavailable
  inside an evidence transaction. Backends gained the matching
  `delete_bindings` operation on all three implementations.

- Added a command mode to `scripts/test-postgres.sh`: everything after a `--`
  separator runs against the scratch server instead of dr-store's own pytest,
  with `DR_STORE_POSTGRES_DSN` exported, the exit code propagated, and the same
  teardown on success, failure, and interrupt. Consumer repositories can reuse
  the scratch-server mechanics without duplicating them. Invocation without
  `--` is unchanged.

- Added the `dr_store.artifact_bundle` package and its fifteen public names
  (`ArtifactBundlePublication`, `ArtifactBundleReader`, `BundleManifest`,
  `ArtifactDescriptor`, `BundleArtifactWriter`, `VerifyingArtifactReader`,
  `BundleReadLimits`, and the `ArtifactBundleError` hierarchy) for terminal
  manifest-committed publication and bounded verified reads of one task, run,
  or result directory. The on-disk `dr-store-artifact-bundle-v1` manifest
  format is byte-identical to the one written by 0.2.0, so bundles recorded by
  that release read unchanged.
- Added `.defs` contract and term entries covering the bundle manifest format,
  manifest-committed publication, layered bundle reads, verified consumption,
  and the bundle failure taxonomy.
- Added `pydantic>=2.0` as a declared runtime dependency; the bundle boundary
  models import it directly rather than relying on a transitive resolution.
- Added `dr_store.document_file.is_reserved_document_temp_name(name)`, the
  public predicate for the case-insensitive `.dr-store-document-` namespace
  that manifest publication reserves. Bundle artifact names, document-directory
  sidecar names, and canonical document names are all refused through it, so
  one source of truth covers every layer that creates children beside a
  published document.

### Changed

- Artifact bundles are a thin packaging layer over existing storage
  primitives. Manifest publication runs as one `DocumentDirectory` manifest
  publication, and every declared artifact is recovered by exactly one
  `read_verified_regular_child` call, so the package holds no second
  atomic-replacement and no second verified-read implementation.
- `ArtifactBundleReader.consume_and_verify_artifact` performs one bounded
  verified whole-read before invoking its consumer, so a selected artifact
  must fit within `max_bytes_per_artifact` and `max_total_artifact_bytes` and
  bytes that fail descriptor verification are never delivered.
- The document-directory granularity contract now names artifact bundles as
  one publication packaged over a document directory.
- Bundle publication bounds the encoded manifest by a fixed 1 GiB package
  ceiling and refuses a larger manifest at `encode_manifest` with
  `ReplacementState.NOT_REPLACED`, while readers keep bounding the manifest by
  their own `BundleReadLimits.manifest_max_bytes`.

## [0.2.2] - 2026-08-12

### Added

- Added sync `install_postgres_sync(engine)` and `PostgresBackend.open_sync(engine)`
  for sync-first platform assembly without an `AsyncEngine`.
- Added sync PostgreSQL enlisted methods (`*_enlisted`) that run on a
  caller-owned SQLAlchemy Core `Connection` without commit, rollback, or
  release.
- Added sync `ObjectStore` enlisted helpers (`put_enlisted`, `bind_enlisted`,
  `put_many_enlisted`, `get_bound_objects_enlisted`, `get_enlisted`,
  `get_many_enlisted`, `resolve_enlisted`) for ledger-coordinated checkpoint
  writes and caller-owned sync reads.
- Added pinned object-reference wire format via `OBJECT_REFERENCE_PREFIX`,
  `format_object_reference`, and `parse_object_reference`.

### Changed

- Hard-cut PostgreSQL enlistment from async `AsyncConnection` to sync Core
  `Connection` for platform checkpoint composition.
- Async `install_postgres` and `PostgresBackend.open` remain for awaited
  auto-acquire operations; backends opened via `open_sync` raise on those paths.

### Removed

- Removed the optional async `connection=` kwarg from PostgreSQL backend
  auto-acquire methods.

## [0.2.1] - 2026-08-12

### Added

- Added typed `DocumentReadError` reporting with `ReadStage` and `RegularChildFailureReason`.
- Added `RecordCacheStats` and corruption logging on unverifiable cache reads.
- Added `ContentMismatchReason` for typed object verification failures.
- Added `read_verified_regular_child` and `VerifiedRegularChildReadError` as the
  storage-owned bounded descriptor-pinned read-and-verify primitive for regular
  direct children.
- Added public `ObjectStore.get_many` and `ObjectStore.put_many` for
  evidence-grade bulk reads and prepared bulk writes. Bulk hits use `StoreHit`
  so bound strict-JSON `null` records are distinct from unbound keys.
- Added public `ObjectStore.verify_stored_record` so callers can verify stored
  content separately from joined reads.
- Added shared `descriptor_io` helpers and consolidated safe-name validation in
  `core/filesystem.py`.
- Added `@verify(UNIQUE)` on `ContentMismatchReason` and
  `RegularChildFailureReason`.
- Added a DB-free golden pin for `POSTGRES_METADATA` table layout.
- Added the `unbudgeted` term and renamed the shared vocabulary entry to
  `canonical JSON text`.

### Changed

- Unified regular direct-child read and sidecar verification failures under
  `RegularChildFailureReason`, replacing message-based failure classification.
- Consolidated pinned-read internals into `core/filesystem.py` and
  `core/descriptor_io.py`; canonical document byte-bound failures now report
  `ReadStage.READ_BYTES` rather than `ReadStage.DECODE`.
- Hard-cut the PostgreSQL backend from `asyncpg` to SQLAlchemy async with
  psycopg. `install_postgres(engine)` and `PostgresBackend.open(engine)` accept
  a caller-owned `AsyncEngine`; exported `POSTGRES_METADATA` defines the fixed
  `dr_store` tables for platform Alembic ownership while retaining the
  `dr-store-postgresql-v1` marker.
- Added optional explicit SQLAlchemy Core `connection=` on PostgreSQL backend
  methods so evidence reads and writes can join a caller-owned transaction;
  enlisted `get_bound_objects` observes one caller-transaction snapshot.
- Exposed `batch_chunk_size` on `PostgresBackend.open` and `busy_timeout_ms` on
  `SqliteBackend.open` and `SqliteRecordCache.open` as constructor knobs with
  documented defaults.
- Hard-cut document publication to visibility-only same-directory replacement
  without `F_FULLFSYNC`, `fsync`, or directory flush.
- Removed `FLUSH_TEMP` and `FLUSH_DIRECTORY` from `PublicationStage`.
- Flattened `ManifestPublishError` and `ManifestReadError` onto the same
  structured fields as delegated document-file errors.
- Typed `ContentHashMismatchError` with `ContentMismatchReason`; removed
  diagnostic sentinel strings from `actual`.
- Typed `SidecarVerificationError` with `path` and `RegularChildFailureReason`.
- `DocumentDirectory.verify_sidecar` now delegates to
  `read_verified_regular_child` while preserving its verify-only surface.
- Parametrized shared backend conformance tests across memory, sqlite, and
  optional postgres instead of hand-maintained re-exports.
- Rewrote `ControlledBackend` test instrumentation as a wrapper over
  `MemoryBackend`.
- Renamed cross-package validators to public `validate_binding_key`,
  `validate_content_hash`, and `validate_reference_schema`.
- PostgreSQL batch closures now return their results directly instead of mutating
  outer dicts.
- Relocated Object Store and Record Cache error semantics in the README to
  their subsystem sections.

### Removed

- Removed `PutOutcome.stored_schema` and `BoundObjectRow.object_schema`; callers
  test `canonical is None` for missing object rows.
- Removed dead state: `SqliteBackend._path`, duplicate
  `SqliteRecordCache._active_operations`, discarded publication
  `directory_close_failed`, and the asyncpg-era `postgres_pool` fixture alias.
- Removed duplicate `_Lifecycle` and `ReadBoundsExceededMarker`.
- Removed the artifact-bundle public API and package.
- Removed the unused `pydantic` runtime dependency left after the bundle cutover.

- Split `core/errors.py` into `core/reasons.py`, `core/store_errors.py`,
  `core/directory_errors.py`, and `core/verified_read_errors.py`; `core/errors.py`
  remains a compatibility re-export facade.
- Renamed internal PostgreSQL `_run_connection_operation(..., transactional=...)`
  to `write=...` for mutating connection paths.

### Breaking

- Renamed public `ObjectStore.get_bound_rows` to `get_bound_objects`.
- Removed public `ReadReason`; document read failures now report
  `RegularChildFailureReason`.
- Removed redundant protocol fields noted above without compatibility shims.

## [0.2.0] - 2026-08-08

### Added

- Added explicit PostgreSQL 16 through 18 installation and a shared
  asynchronous backend over a caller-owned `asyncpg.Pool`. Installation pins
  exact text identity and one `dr-store-postgresql-v1` schema-format marker;
  `await PostgresBackend.open(pool)` validates that marker before returning.
- Added terminal artifact-bundle publication with independently finalized raw
  artifact writers, a closed canonical manifest format, and typed publication
  failures.
- Added bounded artifact-bundle audits and single-pass verified artifact
  consumption through descriptor-pinned no-follow reads.

### Changed

- Hard-cut the backend, Object Store, and Record Cache operations to awaited
  interfaces. SQLite now owns one loop-affine dedicated worker and connection,
  uses WAL with `synchronous=NORMAL`, and settles admitted work and resource
  cleanup before cancellation returns.
- Renamed Sidecar digest terminology and fields to Sidecar hash terminology as
  a hard public cutover.

### Fixed

- Failed artifact writes now best-effort close their writer-owned descriptor
  while preserving the original write failure and poisoning publication.
- PostgreSQL operations settle transaction and pool-release cleanup under
  cancellation, and bundle reads preserve operational and consumer error
  ownership through their typed surfaces.

## [0.1.5] - 2026-08-06

### Added

- Added `CacheEntry`, `RecordCache.get_many`, and `RecordCache.put_many` for
  exact-key batch cache access. Batch reads return one per-key hit or miss for
  every distinct requested key; batch puts return each key's first binding
  winner. Added `BoundObjectRow`, `BoundObjectWrite`, and the backend batch
  primitives that carry joined reads and prepared writes.

### Changed

- Record Cache operations prepare or verify each entry once through shared
  single and bulk paths. SQLite performs chunked joined exact-key reads without
  claiming a batch-wide snapshot, and executes each non-empty batch write in
  one immediate transaction with rollback on failure. Managed SQLite lifecycle
  admission and shutdown cover complete bulk operations. Cache scheduling,
  dirty tracking, key enumeration, and prefix-query policy remain caller-owned;
  SQLite persistence still does not promise power-loss durability.

## [0.1.4] - 2026-08-06

### Added

- Added `SqliteRecordCache(path)` as the managed persistent Record Cache. It
  initializes storage before returning, admits complete cache operations while
  open, rejects new work and waits for admitted work during shutdown, and
  closes every connection tracked by that instance in the current process.
  Normal and exceptional context exits perform close; a body exception is not
  suppressed when cleanup succeeds, while cleanup failure raises the typed
  close error. Successful repeated and concurrent closes are idempotent. Typed
  errors distinguish post-close use from terminal cleanup failure, committed
  records persist across reopen, and instances and processes retain independent
  lifecycles. One constructor must initialize a new database path before
  concurrent constructors use it.
- Added `CanonicalJsonFile` as the standalone bounded canonical-document
  capability. It publishes through reserved unique same-directory temporary
  files with private permissions, reports typed phase and explicit known,
  completed, or unknown replacement state on failure, and provides
  strict descriptor-pinned reads. Concurrent publishers use independent
  temporary files, and the last successful replacement is authoritative.

### Changed

- `DocumentDirectory` requires a caller-owned `manifest_max_bytes`, accepts an
  optional `manifest_max_depth`, and delegates publication and instance
  `read_manifest()` calls to the standalone canonical-document capability.
  Manifest errors retain the directory taxonomy while preserving the
  standalone typed failure as their cause.
- Canonical files, Document Directories, SQLite backends, and managed SQLite
  caches capture absolute filesystem paths at construction. SQLite
  storage rejects empty and `:memory:` paths.
- New public callable annotations are runtime-resolvable, including canonical
  document errors and the managed SQLite cache surface.
- Canonical document and Manifest reads are bounded, require exact canonical
  strict JSON bytes from a regular direct child, reject final-component
  symlinks, and remain pinned to the descriptor they inspected across a
  concurrent replacement.
- Reserved the case-insensitive `.dr-store-document-` prefix for canonical
  document publication temporary files. Standalone document targets, Manifest
  names, and Document Directory Sidecars cannot use that prefix.
- Pinned CI, Pages, release, and pre-commit dependencies to reviewed immutable
  commits, including every action used by the trusted PyPI publication job.

### Fixed

- Restricted macOS `F_FULLFSYNC` fallback to unsupported-operation errors so
  I/O, bad-descriptor, interrupted, and unclassified flush failures propagate
  instead of being hidden by a successful `fsync` fallback.
- Close interruption before connection cleanup begins restores an open
  managed-cache lifecycle and wakes another closer; process-level cleanup
  failure publishes a terminal lifecycle before it propagates. Connection
  cleanup now attempts every owned connection even when one close raises a
  process-level interruption. Removed the unused path-based `flush_directory()`
  primitive.

## [0.1.3] - 2026-08-05

### Added

- `RecordCache` memoizes records over an existing `ObjectStore` under opaque
  caller-owned keys, returning a typed `CacheHit` so strict JSON null remains
  distinct from a miss. `derive_cache_key` provides a canonical key scheme from
  a caller-owned namespace and payload, rejecting non-string namespaces rather
  than coercing them. Reads report absent, missing, and unverifiable stored
  values as misses while invalid requested schemas and operational backend
  faults raise. A conflicting put returns the existing winner, and there is no
  delete, expiry, or eviction path.

### Changed

- Clustered the Record Cache implementation in its own source subpackage and
  documented each functional area through stable public contract shapes in the
  README.
- Required release tags to name commits contained in `main` and made the PyPI
  workflow publish the exact wheel exercised by the canonical pre-check with
  the source distribution produced in the same build.
- Added structural validation for binding contract entries rendered by the
  Definitions page.

## [0.1.2] - 2026-08-05

### Changed

- `DocumentDirectory.verify_sidecar` accepts a lexically valid direct-child
  Sidecar name on a directory instance instead of an arbitrary path.
  Verification opens the Document Directory as its authority, refuses
  final-component symlinks for both that directory and its named child,
  requires a regular child file, and streams bounded reads from the exact
  descriptor it inspected. Platforms without directory-relative no-follow
  opens fail closed.
- Required dr-serialize 0.1.2 and enforced its Canonical JSON Text profile
  bounds before Object Store writes and Manifest publication. Caller records
  outside the profile fail before reaching a backend; stored objects outside
  the profile raise `ContentHashMismatchError`, while Manifest publication and
  read-back raise `ManifestPublishError` and `ManifestReadError`, respectively.
- Reorganized package source and tests around the top-level functional areas
  `content_addressing`, `object_store`, `storage_backends`, and
  `document_directory`, with supporting errors and filesystem mechanics under
  `core`. The root `dr_store` export names remain unchanged. Internal module
  paths are a hard cutover with no compatibility aliases; existing pickle
  payloads tied to the previous defining modules are incompatible with this
  layout.
- Reworked the README around the package's functional capabilities, filesystem
  semantics, and failure boundaries, and migrated Definitions from a
  hand-authored page to authoritative TOML terms and contracts rendered in the
  browser.
- Replaced scheduler-dependent tests with direct backend contracts, explicit
  synchronization gates, scoped process-death cases, and exact isolated-wheel
  layout verification.
- Consolidated local and CI validation behind one canonical pre-check, added a
  repository-local pre-commit hook that delegates to it, retained Depot-backed
  CI runners, and added tag-gated release checks for 0.1.2.

### Fixed

- Translated stored JSON parser limit failures into
  `ContentHashMismatchError` and rejected negative expected Sidecar segment
  lengths during verification.
- Corrected the README, Definitions page, and 0.1.0 and 0.1.1 release notes to
  distinguish persistence, same-directory replacement visibility, and
  descriptor flushing from power-loss durability, and to document
  final-component symlink and failed Sidecar-writer limitations.

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
