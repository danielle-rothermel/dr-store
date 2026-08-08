# Async Shared Records and Artifact Bundles

Status: agreed design plan for local refinement; implementation has not started.

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

## Agreed design decisions

### One async record stack

`Backend`, `ObjectStore`, and `RecordCache` keep their existing public names.
Every storage-touching operation hard-cuts to one awaited API; there are no
synchronous aliases or parallel `Async*` types.

Memory, SQLite, and PostgreSQL implement the same backend semantics and shared
conformance suite. Cancellation, cleanup, replay, first-writer behavior, and
persistence scope are governed by the proposed replacement and new contracts.

This keeps async behavior at the capability boundary instead of making it a
backend-specific variant, and avoids two public surfaces whose semantics could
drift.

### SQLite execution and lifecycle

SQLite uses one connection-affine worker per backend and explicit awaited open
and close lifecycles for the backend and managed cache. The exact operation,
cancellation, ownership, and terminal-close guarantees live in the
[updated contracts](update-contracts.toml); the corresponding managed-cache
concept lives in the [updated terms](update-terms.toml).

This topology favors explicit resource ownership, predictable cancellation and
shutdown, and one state-synchronized lifecycle over executor-wide shared state.

### PostgreSQL adapter and installation

PostgreSQL uses one required driver's concrete caller-owned async pool, an
explicit absent-only installation into a fixed namespace, and deterministic
exact-text identity independent of deployment defaults. The backend introduces
no managed pool, connection-source Protocol, configurable namespace, or initial
migration framework. The exact ownership, installation, credential, and storage
guarantees live in the [new contracts](plan-contracts.toml), and the capability
definition lives in the [new terms](plan-terms.toml).

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

Bundle publication stores complete caller-supplied artifact bytes and admits one
active artifact writer. Retention, truncation, and production coordination stay
outside the bundle capability. Exact publication and descriptor behavior lives
in the [new contracts](plan-contracts.toml).

This keeps bundle publication a small deterministic state machine rather than a
concurrent scheduling API.

### Eager, bounded bundle reads

A bundle read eagerly verifies all declared content under caller-supplied bounds
and returns verified metadata with ordinary local paths. Verification remains a
point-in-time guarantee rather than protection against later external filesystem
mutation. The `artifact bundle read` concept lives in the
[new terms](plan-terms.toml), and its exact bounds and descriptor-pinned behavior
live in the [new contracts](plan-contracts.toml).

Eager verification gives callers one unambiguous accepted state. Caller-owned
limits keep untrusted input bounded without imposing one package-wide size
policy on legitimate large artifacts.

### Bundle error surface

Bundle allocation, terminal publication, incomplete reads, and artifact
verification use one small typed hierarchy with structured recovery context.
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
- Give SQLite its dedicated-worker topology and explicit async lifecycle.
- Preserve the semantics governed by the proposed replacement terms and
  contracts through shared conformance tests.
- Update exports, documentation, examples, and tests without compatibility
  paths.

This PR is independently releasable without PostgreSQL. Downstream packages
remain pinned until their own async hard cutovers are planned.

### PR 2: add the PostgreSQL backend

- Select and pin the concrete async driver and public pool type within the
  agreed required-dependency boundary.
- Add explicit fixed-namespace installation and the PostgreSQL backend.
- Pin deterministic exact-text behavior in the schema.
- Validate the shared backend contracts against password-authenticated
  PostgreSQL, including transaction, cancellation, connection-release,
  contention, equality, and credential-boundary cases.
- Land the implemented PostgreSQL terms and contracts with public symbol
  mappings and evidence checks.

### PR 3: add artifact bundles

- Add the closed versioned manifest models, single-writer publisher, bounded
  eager reader, verified result, and public error hierarchy.
- Keep `DocumentDirectory` public with its distinct mutable lifecycle.
- Add state-synchronized interrupted-publication and concurrency tests.
- Apply the sidecar-hash hard cutover to fields, parameters, documentation, and
  tests in the same PR.
- Land the implemented artifact-bundle terms and contracts with public symbol
  mappings and evidence checks.

Each PR must satisfy its own contracts; a later PR is never required to make an
earlier PR truthful.

## Remaining implementation selections

The architecture has no remaining discussion question from this design pass.
Two concrete selections are intentionally made inside their owning PR and must
not change the decisions above:

1. PR 2 selects the exact async PostgreSQL driver, pool type, minimum version,
   and lockfile entry. The result must remain one concrete required dependency
   and one caller-owned pool boundary.
2. PR 2 selects the exact PostgreSQL DDL expression that supplies deterministic
   binary-like text comparison on the supported PostgreSQL versions. Tests must
   pin exact case-sensitive and non-ASCII identity behavior rather than relying
   on the deployment default.

If either selection cannot satisfy its boundary without expanding scope, PR 2
stops for design review rather than introducing an optional dependency, custom
Protocol, configurable namespace, or byte-valued public identity.

## Validation and handoff

Contract entries receive runnable `check` commands only with their implementing
PR. Tests synchronize on explicit state, database locks, worker gates, or exact
terminal outcomes; elapsed time is only a watchdog. Persisted bundle literals
receive golden tests, and all backend implementations run the same semantic
conformance suite.

After each PR is released, downstream integration plans inspect that release's
actual public contracts. `dr-platform`, `dr-code`, and `dr-exec` update their
pinned dependency and callers together rather than relying on assumptions from
this planning snapshot.
