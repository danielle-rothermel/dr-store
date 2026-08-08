# Async Shared Records and Artifact Bundles

Status: agreed design plan with implementation selections frozen; implementation
has not started.

## Planning sources and ownership

The repository vocabulary and standing behavioral rules remain authoritative:

- [repository terms](../../../.defs/terms.toml)
- [repository contracts](../../../.defs/contracts.toml)

The following files are the authoritative proposals for this plan. They stage
the vocabulary and contracts that will move into `.defs` with their implementing
PRs:

- [plan-terms.toml](plan-terms.toml) contains new PostgreSQL and
  artifact-bundle terms.
- [update-terms.toml](update-terms.toml) contains complete replacements for
  existing terms changed by the async and sidecar-hash hard cutovers.
- [plan-contracts.toml](plan-contracts.toml) contains new PostgreSQL and
  artifact-bundle contracts.
- [update-contracts.toml](update-contracts.toml) contains replacements and new
  lifecycle contracts required by async storage and sidecar-hash behavior.

Historical design provenance: the
[performance and cross-plan audit](perf-and-plan-audit-review-doc.md) records
the review that produced the performance and ownership revisions incorporated
into this plan. This plan and its companion TOML files remain the current
design sources.

This document owns motivation, design selections, delivery order, and
implementation boundaries. It does not redefine the terms or restate the exact
guarantees owned by those TOML files. When summary wording here is less precise,
the terms and contracts govern.

## Goal and scope

Make `dr-store` the canonical owner of two independent foundational
capabilities:

1. one asynchronous content-addressed record and cache stack with memory,
   SQLite, and shared PostgreSQL backends; and
2. one terminal local artifact-bundle publication format for a canonical
   manifest and raw-byte artifacts.

The existing record stack is replaced in place by its async form. Local
filesystem publication stays synchronous and does not gain an async twin.

The scope excludes workflow scheduling, automatic retries, provider attempts,
evaluation or experiment schemas, dataframe formats, automatic retention or
cleanup, bundle registration or whole-bundle identity, remote blob storage,
schema migration machinery, and compatibility paths for superseded development
APIs.

## Cross-repository ownership

`dr-store` owns async content-addressed records, immutable cache bindings,
storage backends, and the generic local artifact-bundle format. It does not
depend on its consumers or embed their payload schemas.

- `dr-providers` owns storage-neutral provider requests, invocation evidence,
  retry transitions, and provider-call results.
- `dr-exec` owns process execution records and their mutable lifecycle. Neither
  the record cache nor directory artifact bundles are its high-volume packed
  execution-record backend.
- `dr-platform` owns durable timers, admission, fan-out, fan-in, workflow
  membership, and persistence of references to foundation-owned results.
- `dr-code` and Whetstone own evaluation rows, task selection, rewards,
  experiment membership, and analysis.

## Agreed design decisions

### One async record stack

`Backend`, `ObjectStore`, and `RecordCache` keep their existing public names.
Every storage-touching operation hard-cuts to one awaited API; there are no
synchronous aliases or parallel `Async*` types.

Memory, SQLite, and PostgreSQL implement the same backend semantics and shared
conformance suite. Exact content verification remains at every record-storage
trust boundary. Cancellation, cleanup, replay, first-writer behavior, and
persistence scope are governed by the proposed replacement and new contracts.
Schemas remain non-empty, keys may be empty, and both text domains reject NUL
and unpaired surrogate code points through every backend and facade before
storage work begins.

This keeps async behavior at the capability boundary instead of making it a
backend-specific variant, and avoids two public surfaces whose semantics could
drift.

### SQLite execution and lifecycle

SQLite uses one connection-affine worker per long-lived backend, admits no more
than one submitted or running worker operation, and gates other callers before
submission. Its resources are bound to the event loop that opens them, and
cross-loop use fails visibly. Cancellation after admission waits for worker work
and cleanup to settle before cancellation wins, retaining a worker failure as
its cause. A raw `SqliteBackend` operation after close raises `RuntimeError`;
there is no new public backend-closed exception. WAL `synchronous=NORMAL`
matches the bounded persistence claim, and hash-leading object lookup prevents
alternate-schema resolution from scanning the object table. Exact admission,
cancellation, lifecycle, synchronization, and indexing guarantees live in the
[updated contracts](update-contracts.toml); the corresponding managed-cache
concept lives in the [updated terms](update-terms.toml).

This topology favors explicit resource ownership, predictable cancellation and
shutdown, and one state-synchronized lifecycle over executor-wide shared state.

### PostgreSQL adapter and installation

PostgreSQL uses required `asyncpg>=0.31.0` and accepts one concrete caller-owned
`asyncpg.Pool`. Public `install_postgres` performs absent-only installation into
the fixed `dr_store` namespace, and public `PostgresBackend` supplies storage.
The supported database boundary is PostgreSQL 16 through 18 with UTF-8 server
encoding, and schema and key text use `pg_catalog.ucs_basic` for deterministic
identity independent of deployment defaults. The backend introduces no optional
import, managed pool, DSN ownership, connection-source Protocol, configurable
namespace, or migration framework. Its hash-leading object key and set-based,
chunked batch operations prevent hash-only scans, per-entry round trips, and
repeated transfer of one canonical object for multiple bindings. The exact
ownership, installation, credential, storage, and batch guarantees live in the
[new contracts](plan-contracts.toml), and the capability definition lives in the
[new terms](plan-terms.toml).

These choices keep deployment and pool lifecycle with the application while
giving storage identity the same meaning in every supported PostgreSQL
environment.

### Document directories and artifact bundles remain distinct

`DocumentDirectory` remains a public mutable publication primitive for work in
progress. An artifact bundle is a separate terminal publication whose supported
API exposes no mutation after the manifest-publication transition. Neither
replaces or adapts the other. Their exact definitions live in the
[updated terms](update-terms.toml) and [new terms](plan-terms.toml).

This preserves two different lifecycles rather than forcing ongoing output into
an immutable-result abstraction or weakening bundle completion semantics.

### Closed, versioned bundle format

The bundle uses one closed, self-identifying manifest format with a fixed name,
explicit persisted literals, deterministic descriptor order, strict boundary
models, and fail-closed format handling. The exact filename, format marker, wire
keys, and ordering live only in the [new contracts](plan-contracts.toml).

The envelope gives storage-owned integrity metadata and caller-owned domain
metadata separate owners. An explicit format marker lets readers fail closed
without adding speculative compatibility readers.

### Artifact writing and retention

Bundle publication stores complete caller-supplied artifact bytes and admits
independent concurrent writers for distinct exact names. Each synchronous
writer owns one file and incremental hash; the bundle owns thread-safe name and
state coordination but schedules no tasks and multiplexes no bytes. A publish
call made with an active writer or invalid payload is a nonterminal precondition
refusal. A failed admitted writer permanently prevents publication. The
publication becomes terminal only when a valid publish call after all writers
finalized successfully begins the manifest attempt. Retention and truncation
remain outside the capability. Exact writer, publication, and descriptor
behavior lives in the [new contracts](plan-contracts.toml).

This supports concurrent stdout/stderr-shaped producers without turning bundle
publication into a scheduling API.

### Integrity audit and one-pass consumption

Bundle reads validate and pin the manifest under caller-supplied bounds. An
explicit integrity audit eagerly verifies every declared artifact. A separate
synchronous verified-consumption operation delivers one selected artifact to a
consumer callback through a package-owned facade while hashing and counting the
same bytes; only EOF with matching hash and length is verified success. Public
`ArtifactBundleReader(path, limits)` supplies `audit()` and
`consume_and_verify_artifact(...)`; the latter gives the callback a
`VerifyingArtifactReader` exposing only `read(...)`. `BundleReadLimits` owns the
caller bounds.

The exact read concepts live in the [new terms](plan-terms.toml), and their
bounds, descriptor lifetime, EOF, and point-in-time guarantees live in the
[new contracts](plan-contracts.toml). This preserves whole-bundle audit without
forcing consumers of large artifacts through a verify-then-read double pass.

### Visibility-only publication

Artifact writers flush userspace buffers and close their files. Manifest
publication writes and closes a same-directory temporary file and atomically
replaces the fixed manifest. The default bundle path performs no full file or
directory synchronization and exposes no durability mode.

The exact visibility and failure boundary lives in the
[new contracts](plan-contracts.toml). A future crash-durable publication is a
separate capability rather than an option on this one.

### Synchronous boundary and directory-format ceiling

Artifact bundle operations stay synchronous. Async applications offload one
complete writer, publication, audit, or consumption operation rather than
running package hashing or filesystem I/O on an event-loop thread.

One bundle represents one task, run, or result publication, not an event or
packed execution record. Callers own partitioned allocation roots and retention.
Sustained creation near 100,000 bundles per hour is outside this local directory
format and requires aggregation or another blob-storage capability. These
boundaries live in the [new terms](plan-terms.toml) and
[new contracts](plan-contracts.toml).

### Bundle error surface

Bundle allocation, terminal publication, incomplete reads, and artifact
verification use one small typed hierarchy with structured recovery context.
`BundlePublicationPhase` and `BundleVerificationReason` keep the already-agreed
structured fields public and stable, while manifest replacement knowledge reuses
the existing `ReplacementState`.
The public failure concepts and symbol mappings live in the
[new terms](plan-terms.toml); the exact hierarchy and exception-chaining behavior
live in the [new contracts](plan-contracts.toml).

This distinguishes the recovery-relevant failure categories without freezing
an exception type for every implementation step.

### Sidecar hash hard cutover

Sidecar stored-byte verification uses sidecar hash consistently in public
vocabulary and APIs. Bounded head/tail behavior remains governed by the
[updated term](update-terms.toml) and [updated contracts](update-contracts.toml).
The PR lands one canonical name with no aliases or compatibility fields.

## Three-PR delivery stack

### PR 1: hard-cut the existing record stack to async

- Replace the public record-stack operations with their awaited forms.
- Add `pytest-asyncio` as a direct development dependency for asynchronous
  tests.
- Give SQLite its bounded dedicated-worker admission and explicit long-lived
  async lifecycle.
- Use WAL `synchronous=NORMAL`, a hash-leading object key or index, and prove
  missing and wrong-schema query plans avoid object-table scans.
- Preserve the semantics governed by the proposed replacement terms and
  contracts through shared conformance tests.
- Update exports, documentation, examples, and tests without compatibility
  paths.

This PR is independently releasable without PostgreSQL. Downstream packages
remain pinned until their own async hard cutovers are planned.

### PR 2: add the PostgreSQL backend

- Add required `asyncpg>=0.31.0` and accept caller-owned `asyncpg.Pool` only.
- Add public `install_postgres` and `PostgresBackend` for PostgreSQL 16 through
  18 UTF-8 databases and the fixed `dr_store` namespace.
- Pin `pg_catalog.ucs_basic` exact-text behavior and hash-leading object lookup
  in the schema.
- Implement set-based, bounded-chunk batch operations that fetch distinct
  objects separately and reconstruct binding results in Python.
- Validate the shared backend contracts against password-authenticated
  PostgreSQL, including transaction, cancellation, connection-release,
  contention, equality, credential-boundary, round-trip-count, and
  repeated-reference transfer cases.
- Land the implemented PostgreSQL terms and contracts with public symbol
  mappings and evidence checks.

### PR 3: add artifact bundles

- Add strict frozen `ArtifactDescriptor` and `BundleManifest` models;
  `ArtifactBundlePublication.allocate/open_artifact/publish`;
  `BundleArtifactWriter.write/finalize`; `ArtifactBundleReader(path, limits)`
  with `audit` and `consume_and_verify_artifact`; `BundleReadLimits`; the
  read-only `VerifyingArtifactReader` callback facade; and the public error
  hierarchy.
- Publish through close and atomic manifest replacement without file or
  directory synchronization or a durability mode.
- Keep `DocumentDirectory` public with its distinct mutable lifecycle.
- Add async-offload guidance and state the directory-format workload ceiling.
- Add state-synchronized interrupted-publication, writer-concurrency, EOF, and
  single-read tests.
- Apply the sidecar-hash hard cutover to fields, parameters, documentation, and
  tests in the same PR.
- Land the implemented artifact-bundle terms and contracts with public symbol
  mappings and evidence checks.

Each PR must satisfy its own contracts; a later PR is never required to make an
earlier PR truthful.

## Frozen implementation selections

This design pass leaves no implementation selection unresolved. PR 1 adds
`pytest-asyncio` directly to the development dependency group. PR 2 uses required
`asyncpg>=0.31.0`, concrete caller-owned `asyncpg.Pool`, PostgreSQL 16 through 18
with UTF-8 server encoding, `pg_catalog.ucs_basic`, public `PostgresBackend`, and
public `install_postgres`. PR 3 uses the exact bundle symbols and methods listed
above and in [plan-terms.toml](plan-terms.toml).

If one of these selections cannot satisfy its committed boundary without
expanding scope, its owning PR stops for design review rather than introducing
an optional dependency, Protocol, DSN ownership, configurable namespace,
migration framework, compatibility path, or an async bundle twin.

## Validation and handoff

Contract entries receive runnable `check` commands only with their implementing
PR. Tests synchronize on explicit state, database locks, worker gates, or exact
terminal outcomes; elapsed time is only a watchdog. Persisted bundle literals
receive golden tests, and all backend implementations run the same semantic
conformance suite. Structural instrumentation, not elapsed-time assertions,
proves database round-trip and artifact-read complexity. Wall-clock throughput
belongs in repeatable benchmark or investigation scripts.

The implementation handoff reports:

- SQLite query-plan evidence and synchronization mode;
- PostgreSQL statement counts for a small and a multi-chunk batch;
- proof that verified consumption reads selected artifact content once;
- proof that concurrent stdout/stderr-shaped writers cannot publish early; and
- filesystem object and byte counts for a representative task/run bundle.

After each PR is released, downstream integration plans inspect that release's
actual public contracts. `dr-platform`, `dr-code`, and `dr-exec` update their
pinned dependency and callers together rather than relying on assumptions from
this planning snapshot.
