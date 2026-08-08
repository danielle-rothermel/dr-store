# Shared Records and Artifact Bundles

Status: design plan for local refinement; implementation has not started.

## Purpose

Make `dr-store` the canonical owner of two storage primitives needed by the
research stack:

1. a shared typed record/cache backend that independent processes and machines
   can resolve consistently; and
2. an immutable, verifiable bundle for publishing a manifest with large
   sidecar artifacts.

The first primitive lets a durable workflow persist references instead of
embedding large values in workflow state. The second gives experiment and
evaluation code one general publication format without making `dr-store` own
the meaning of evaluation evidence.

## Existing foundation

`dr-store` already owns the relevant conceptual layers:

- `ObjectStore` provides content-addressed objects and named bindings over a
  backend.
- `RecordCache` provides typed record caching over `ObjectStore`.
- The in-memory and SQLite backends establish the local semantic contract.
- `DocumentDirectory` allocates a directory and supports a manifest plus
  streamed sidecars.

The missing pieces are a backend suitable for shared deployments and a
complete immutable-publication contract above the directory primitive.

## Intended ownership

### Shared record backend

Add a PostgreSQL implementation of the existing backend contract. It must
preserve the same observable storage semantics as the in-memory and SQLite
implementations rather than expose database-specific behavior to callers.

The design must settle and document:

- append-only object insertion and digest collision handling;
- single-assignment named bindings and concurrent conflicts;
- bulk lookup and write outcomes;
- transaction boundaries and connection lifecycle;
- schema creation and migration ownership;
- construction from a DSN, engine, or deliberately smaller connection
  abstraction;
- credential redaction from representations, errors, and persisted metadata;
  and
- semantic conformance tests shared by every backend.

`ObjectStore` and `RecordCache` remain the caller-facing layers. The new
backend should not create a parallel high-level storage API.

### Immutable artifact bundles

Add one general artifact-bundle writer and reader, implemented over
`DocumentDirectory` or as its direct successor. A bundle represents one
completed publication containing:

- one manifest written as the final publication step;
- zero or more safely named sidecar artifacts;
- a descriptor for each artifact containing at least its relative name,
  digest, and byte length;
- optional content metadata only where it is genuinely general; and
- verification on read for missing, truncated, or modified content.

The bundle contract must make partial publication distinguishable from a
complete bundle. It must forbid absolute paths and traversal and define the
policy for unexpected extra files. Allocation must be collision-safe; callers
must not coordinate a last-write-wins directory convention themselves.

The type is an **artifact bundle**, not an evidence bundle. Evaluation,
provider, corpus, and experiment packages own the domain-specific manifest
schemas placed inside it.

## Explicit non-goals

This foundation does not own:

- workflow membership, sealing, fan-in, or scheduling;
- provider-call attempts or retry policy;
- evaluation rows, task identity, statistical analysis, or experiment roles;
- dataframe or Parquet schemas;
- a general distributed filesystem or object-storage service; or
- compatibility layers for superseded development APIs.

S3 and other remote blob backends are deferred unless the local design review
finds that the storage protocol must account for them immediately.

## Design questions to finalize locally

1. Does the PostgreSQL backend own schema installation, require an installed
   schema, or expose an explicit installation operation?
2. What is the narrowest constructor boundary that is safe for both local use
   and dependency injection without leaking credentials?
3. Should bundle descriptors support a small generic media type or schema
   identity field now, or should domain manifests carry all such meaning?
4. Are extra files a verification error, ignored implementation detail, or an
   explicitly enumerated part of the bundle?
5. What durability guarantee does a completed local bundle claim across file
   and directory synchronization?
6. Does `DocumentDirectory` remain a lower-level public primitive, or does the
   completed bundle contract replace its public use in a hard cutover?

## Implementation sequence

1. Freeze the shared backend behavioral contract as conformance tests.
2. Implement and test the PostgreSQL backend against that contract.
3. Freeze the artifact-bundle manifest and publication state machine.
4. Implement the writer, reader, and verification failures.
5. Add concurrency and interrupted-publication tests using explicit state
   synchronization rather than timing assumptions.
6. Update public exports, terminology, contracts, and package documentation.
7. Release a pinned version before downstream packages adopt the primitives.

These may become separate stacked changes if the PostgreSQL and bundle work
are independently reviewable, but they should converge on one coherent
storage vocabulary.

## Validation bar

- Every backend passes the same object and binding conformance suite.
- Concurrent insert/bind tests establish exact terminal outcomes.
- PostgreSQL integration tests run against a real database service.
- Bundle tests cover successful publication, interrupted publication, unsafe
  names, missing artifacts, digest mismatch, size mismatch, and allocation
  collision.
- Logs, errors, identities, and serialized records contain no credentials.
- Public docs state only guarantees demonstrated by the tests.

## Downstream handoff

After this foundation is implemented and released, inspect its final public
contracts before planning `dr-platform` or `dr-code` integration. Those later
plans should consume shared references and immutable bundles as released,
rather than preserve assumptions from this planning document.
