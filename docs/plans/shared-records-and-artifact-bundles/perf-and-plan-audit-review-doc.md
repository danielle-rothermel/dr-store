# Performance and Cross-Plan Audit Instructions

Status: required plan revisions before implementation.

## Purpose

Finalize `dr-store` as the storage foundation for the later `dr-platform`,
`dr-code`, and Whetstone workflows without importing execution, provider, or
evaluation policy. The async record stack and the terminal local artifact
bundle remain separate capabilities.

The implementation plan, proposed terms, and proposed contracts must be updated
to agree with this document before source schemas or public APIs are frozen.

## Cross-repository ownership

- `dr-store` owns async content-addressed records, immutable cache bindings,
  storage backends, and the generic local artifact-bundle format.
- `dr-providers` owns provider requests, invocation evidence, retry transitions,
  and provider-call results. Its models remain storage-neutral.
- `dr-exec` owns process execution records and their lifecycle. A mutable
  execution record is not a `dr-store` record-cache entry, and a directory
  artifact bundle is not the high-volume execution-record backend.
- `dr-platform` will later own durable timers, admission, fan-out, fan-in,
  workflow membership, and persistence of references to foundation-owned
  results.
- `dr-code` and Whetstone will own evaluation rows, task selection, rewards,
  experiment membership, and analysis.

Do not add dependencies from `dr-store` to any of those repositories or add
their payload schemas to the bundle manifest.

## Decisions to retain

Retain the following plan selections:

- one hard-cut async `Backend`/`ObjectStore`/`RecordCache` surface;
- one connection-affine worker and connection per SQLite backend;
- one concrete required async PostgreSQL driver with a caller-owned pool;
- explicit absent-only PostgreSQL installation in the fixed namespace;
- exact content verification at every record-storage trust boundary;
- synchronous artifact-bundle APIs, distinct from `DocumentDirectory`;
- SHA-256 identities, strict manifests, exact artifact names, and typed errors;
  and
- no workflow, retries, retention service, remote blob store, or domain schema.

The async record cutover does not require an async filesystem twin. Async
applications must offload a complete synchronous bundle operation rather than
run hashing or filesystem I/O on an event-loop thread.

## Required record-stack revisions

### Bound SQLite admission before the worker queue

Each SQLite backend admits at most one submitted or running worker operation.
Other callers await an async admission gate before any work is submitted.
Cancellation while waiting starts no SQLite work. Cancellation after admission
does not abandon the connection-affine operation or its cleanup.

The backend and managed cache are long-lived resources. No supported usage path
opens and closes either resource per record operation.

### Align SQLite synchronization with its durability claim

Use WAL with `synchronous=NORMAL` for the async SQLite backend unless an
implementation benchmark demonstrates that it violates an agreed contract.
The contract is committed persistence and valid recovery, not survival of the
last acknowledged transaction across power loss. Do not add public durability
modes in this plan.

### Make hash-only lookup indexed

The object table must have an index whose leading column is `content_hash`.
Prefer `PRIMARY KEY (content_hash, schema)` for both SQLite and PostgreSQL,
because uniqueness remains the same while exact and alternate-schema lookups
are both indexed. Pin the chosen DDL and prove the SQLite query plan does not
scan the object table for a missing or wrong-schema lookup.

### Make PostgreSQL batches set-based

PostgreSQL point methods may use point statements. Batch methods must not loop
over entries with one or more database round trips per item.

- Deduplicate identical object references before sending object data.
- Chunk by explicit driver/statement bounds.
- Fetch requested bindings in set-based statements.
- Fetch the distinct referenced objects separately in set-based statements.
- Reconstruct binding/object results in Python so one canonical record is not
  transferred once per referring key.
- Use one transaction for a non-empty write batch.

Instrument representative batch tests so statement or round-trip counts grow
with the number of bounded chunks, not the number of entries. The semantic
backend conformance suite remains shared; these structural performance tests
are PostgreSQL-specific.

## Required artifact-bundle revisions

### Support integrity audit and one-pass consumption separately

Keep a whole-bundle eager audit operation for callers that explicitly need to
verify every declared artifact. Do not make eager verify-then-return-path the
only consumption path.

Add one synchronous verified-consumption operation with these semantics:

- pin and validate the manifest under caller bounds;
- open the selected direct-child artifact with the same no-follow,
  directory-relative requirements as eager verification;
- deliver bytes from that descriptor while hashing and counting the same bytes;
- report success only after EOF and exact descriptor hash/length verification;
- treat early consumer termination as incomplete consumption, not verified
  success; and
- return verified descriptor metadata, not a claim that a later path read is
  still verified.

A synchronous consumer callback over a package-owned read facade is the
recommended initial shape because it keeps descriptor lifetime and terminal
verification inside one operation and lets async callers offload the complete
operation. The exact public name may be selected in PR 3, but the one-pass and
EOF guarantees must be fixed in the contracts first.

### Allow independent concurrent artifact writers

A bundle admits multiple active writers only for distinct exact artifact names.
Name reservation and writer-state changes are thread-safe. Each writer remains
synchronous, owns one file and incremental SHA-256 state, and finalizes one
descriptor. The bundle schedules no tasks and multiplexes no bytes.

Manifest publication is non-waiting and fails visibly while any admitted writer
is active or failed. It begins only after every admitted writer finalized, then
orders descriptors deterministically and makes the bundle terminal. Tests must
control writer interleavings with barriers or events, never elapsed time.

### Remove default full synchronization

Artifact writers hash the exact supplied bytes, flush userspace buffers, and
close their descriptors. Manifest publication writes and closes a temporary
manifest and atomically replaces `manifest.json`. It does not call
`F_FULLFSYNC`, `fsync`, or a directory flush on the default path.

The completion claim is terminal visibility through the supported API, not
power-loss or crash durability. Do not add configurable durability modes. A
future crash-durable publication capability requires its own design and claim.

### State the directory-format ceiling

Position one bundle as a task, run, or result publication, never a per-event
record. Callers own partitioned allocation roots and retention. The package
must state that sustained creation near 100,000 bundles per hour is outside the
intended directory-format envelope and requires aggregation or another blob
storage capability.

Do not present artifact bundles as the solution to `dr-exec`'s packed-record
requirement: their terminal lifecycle and weaker durability claim are different.

## Delivery instructions

### PR 1: async record stack

In addition to the plan's async hard cutover, include bounded SQLite admission,
`synchronous=NORMAL`, the hash-leading key/index, and cancellation-safe cleanup.
Validate query plans and long-lived lifecycle behavior.

### PR 2: PostgreSQL backend

Use the same hash-leading key order and implement the set-based, chunked batch
shape above. Test round-trip complexity and repeated-reference transfer shape in
addition to semantic conformance, contention, cancellation, and credential
boundaries.

### PR 3: artifact bundles

Land concurrent distinct-name writers, explicit eager audit, one-pass verified
consumption, non-durable atomic manifest publication, async-offload guidance,
and the directory-format workload ceiling together. Update the proposed terms
and contracts before freezing persisted literals.

## Validation evidence

Use state-controlled tests for admission, cancellation, writer concurrency, and
shutdown. Use structural instrumentation for database round trips and artifact
read counts. Keep wall-clock throughput measurements in a repeatable benchmark
or investigation script, not as timing assertions in the ordinary test suite.

The implementation handoff must report:

- SQLite query-plan evidence and synchronization mode;
- PostgreSQL statement counts for at least a small and a multi-chunk batch;
- proof that verified consumption reads artifact content once;
- proof that concurrent stdout/stderr-shaped writers cannot publish early; and
- filesystem object and byte counts for a representative task/run bundle.
