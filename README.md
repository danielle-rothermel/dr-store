# dr-store

[![CI](https://github.com/danielle-rothermel/dr-store/actions/workflows/ci.yml/badge.svg)](https://github.com/danielle-rothermel/dr-store/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dr-store.svg)](https://pypi.org/project/dr-store/)

[Definitions](https://danielle-rothermel.github.io/dr-store/) ·
[Terms](https://github.com/danielle-rothermel/dr-store/blob/main/.defs/terms.toml) ·
[Contracts](https://github.com/danielle-rothermel/dr-store/blob/main/.defs/contracts.toml) ·
[Changelog](https://github.com/danielle-rothermel/dr-store/blob/main/CHANGELOG.md) ·
[dr-serialize](https://github.com/danielle-rothermel/dr-serialize)

dr-store provides domain-neutral storage primitives for immutable records and
document artifacts:

- **[Content addressing](https://github.com/danielle-rothermel/dr-store/blob/main/src/dr_store/content_addressing.py)**
  identifies complete records by their declared schemas and SHA-256 hashes of
  their Canonical JSON Text under dr-serialize's frozen profile.
- **[Object Store](https://github.com/danielle-rothermel/dr-store/blob/main/src/dr_store/object_store.py)**
  provides immutable puts, verified reads, and atomic bindings from opaque
  caller-owned keys to object references.
- **[Storage backends](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/storage_backends)**
  supply the Object Store's atomic point and batch operations over append-only
  object rows and single-assignment bindings.
  `MemoryBackend` is process-local; `SqliteBackend` persists committed data for
  cross-process use; `PostgresBackend` shares committed data through a
  caller-owned SQLAlchemy engine opened synchronously or asynchronously.
- **[Record Cache](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/record_cache)**
  memoizes records under opaque caller-owned keys. Reads return typed hits;
  absent, missing, or unverifiable stored values are misses, while invalid
  requested schemas and operational backend faults raise. Entries are never
  rebound, so callers invalidate by selecting a new key; `derive_cache_key`
  provides a canonical scheme using a versioned namespace and payload.
  Unverifiable stored values still report misses, but increment
  `RecordCache.stats.corruption_count` and log the corrupted key.
  Single and bulk methods share these per-key semantics;
  `await SqliteRecordCache.open(path)` is the managed persistent lifecycle.
- **[Canonical JSON document files](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/document_file)**
  publish and read one standalone, bounded canonical document in an existing
  directory through descriptor-pinned filesystem operations.
- **[Document Directory](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/document_directory)**
  groups one canonical JSON Manifest with streamed binary Sidecars for one task,
  run, or result publication.

## Installation

dr-store requires Python 3.12 or newer.

```console
python -m pip install dr-store
```

PostgreSQL 16 through 18 installations use SQLAlchemy with psycopg and an
explicit, absent-only schema installation step. The caller creates and owns the
engine; dr-store neither accepts a DSN nor disposes the engine.

Sync-first platform assembly (checkpoint enlistment and enlisted reads/writes
on a caller-owned connection):

```python
import os

from sqlalchemy import create_engine

from dr_store import (
    ObjectStore,
    POSTGRES_METADATA,
    PostgresBackend,
    format_object_reference,
    install_postgres_sync,
)

engine = create_engine(
    os.environ["DATABASE_URL"].replace(
        "postgresql://",
        "postgresql+psycopg://",
        1,
    )
)
install_postgres_sync(engine)
backend = PostgresBackend.open_sync(engine)
store = ObjectStore(backend)

# Checkpoint write (inside a DBOS transaction):
# with connection.begin():
#     ref, _ = store.put_enlisted(connection, "example.note.v1", {"title": "hello"})
#     evidence_reference = format_object_reference(ref)

# Read outside a checkpoint (caller opens the connection):
# with engine.connect() as connection:
#     record = store.get_enlisted(connection, ref)
```

Awaited auto-acquire operations such as ``await store.put(...)`` require a
backend opened with ``await PostgresBackend.open(async_engine)``:

```python
import asyncio
import os

from sqlalchemy.ext.asyncio import create_async_engine

from dr_store import (
    ObjectStore,
    POSTGRES_METADATA,
    PostgresBackend,
    install_postgres,
)


async def main() -> None:
    engine = create_async_engine(
        os.environ["DATABASE_URL"].replace(
            "postgresql://",
            "postgresql+psycopg://",
            1,
        )
    )
    try:
        await install_postgres(engine)
        backend = await PostgresBackend.open(engine)
        store = ObjectStore(backend)
        reference, _ = await store.put(
            "example.note.v1", {"title": "hello"}
        )
        assert await store.get(reference) == {"title": "hello"}
    finally:
        await engine.dispose()


asyncio.run(main())
```

`POSTGRES_METADATA` exports the fixed `dr_store` table definitions for platform
Alembic ownership. `install_postgres_sync` and `install_postgres` are one-time
deployment operations that create the namespace, its tables, and the exact
`dr-store-postgresql-v1` schema-format marker in one transaction on a UTF-8
database. Repeating installation is an error.
`PostgresBackend.open_sync(engine)` or `await PostgresBackend.open(engine)`
validates that marker before returning a backend. Backends opened with
``open_sync`` support sync enlisted methods and raise on awaited auto-acquire
operations; backends opened with ``open`` support both paths. Sync enlisted
read helpers (`get_enlisted`, `get_many_enlisted`, `resolve_enlisted`) mirror
the async Object Store read surface on a caller-opened connection, including
outside checkpoint transactions. PostgreSQL sync enlisted methods accept an
``Connection`` so evidence reads and writes can join a caller-owned checkpoint
transaction; dr-store never commits that connection. Enlisted
``get_bound_objects_enlisted`` observes one caller-transaction snapshot.
Enlisted writes share the caller transaction; dr-store defines no savepoint
API. Opening never installs, alters, adopts, or upgrades storage.

## Testing

`scripts/pre-check.sh` is the canonical developer check: lint, types, the full
test suite, and built-wheel layout verification. Without a PostgreSQL DSN the
backend-parametrized tests run against the memory and SQLite backends only and
every PostgreSQL-gated test is skipped.

`scripts/test-postgres.sh` runs the full test suite with the PostgreSQL
backend enabled. It needs no configuration and no existing server: it
provisions a throwaway password-authenticated PostgreSQL server under `/tmp`
(unix socket only, no TCP listener), creates the dedicated `dr_store_test`
database, exports `DR_STORE_POSTGRES_DSN` and `DR_STORE_REQUIRE_POSTGRES=1`
(so PostgreSQL tests fail rather than skip), runs pytest, and tears the server
down. It requires PostgreSQL 16-18 client and server tools, located from
`PATH`, from a Homebrew `postgresql@16`-`18` installation, or from an explicit
`DR_STORE_POSTGRES_BIN=<bin directory>`. Arguments pass through to pytest:

```console
scripts/test-postgres.sh                            # full suite
scripts/test-postgres.sh tests/storage_backends -q  # any pytest selection
```

`scripts/check-compatibility.sh` verifies this working tree against an
already-released dr-store, in both directions, by installing that release into
a throwaway virtual environment and exchanging artifact bundles between the
two. A test suite imports exactly one `dr_store`, so it cannot express these
checks; they need two versions resident at once. It checks that bundles this
tree writes stay readable by the baseline, that bundles the baseline wrote
still read here (including artifact names the baseline admitted but current
publication refuses), and that every public name the baseline exported is
still present.

It is deliberately outside CI and the hooks: it reaches PyPI and spends about
half a minute building the baseline environment. Run it before publishing a
release, and whenever a change touches the bundle format, the public API
surface, or the compatibility claims in `.defs/contracts.toml`.

```console
scripts/check-compatibility.sh                       # against the default baseline
scripts/check-compatibility.sh --baseline 0.2.0      # against a chosen release
scripts/check-compatibility.sh --consumer ../dr-code # also validate a consumer
```

`--consumer PATH` additionally validates a checkout that resolves this working
tree through an editable `[tool.uv.sources]` entry: it confirms the consumer
really imports this tree, runs the consumer's suite, and exercises the
consumer's reuse of `scripts/test-postgres.sh` command mode from its own
directory.

To use an existing server instead, set `DR_STORE_POSTGRES_DSN` to a
`postgresql://` URL whose database is literally named `dr_store_test`: the
test fixtures `DROP SCHEMA dr_store CASCADE` around every test and refuse to
run against any other database name. Set `DR_STORE_REQUIRE_POSTGRES=1` to turn
missing-DSN skips into failures.

## Usage

```python
import asyncio

from dr_store import MemoryBackend, ObjectStore


async def main() -> None:
    store = ObjectStore(MemoryBackend())
    reference, _ = await store.put("example.note.v1", {"title": "hello"})
    await store.bind("notes/latest", reference)

    assert await store.resolve("notes/latest") == reference
    assert await store.get(reference) == {"title": "hello"}


asyncio.run(main())
```

`await SqliteRecordCache.open(path)` is the paved persistent Record Cache. It
returns only after its dedicated worker, connection, and schema are ready and
forwards optional `busy_timeout_ms` to the owned SQLite backend. It closes those
resources on normal or exceptional async context exit. When
cleanup succeeds, an exception from the context body is not suppressed;
cleanup failure raises
`SqliteRecordCacheCloseError`:

```python
import asyncio

from dr_store import CacheEntry, CacheHit, SqliteRecordCache, derive_cache_key


async def main() -> None:
    key = derive_cache_key("example.summary.v1", {"document": "note-42"})
    async with await SqliteRecordCache.open("records.sqlite3") as cache:
        winners = await cache.put_many(
            {
                key: CacheEntry(
                    schema="example.summary.v1",
                    record={"summary": "hello"},
                )
            }
        )
        assert winners[key].schema == "example.summary.v1"
        assert await cache.get_many(
            [key, "missing"], schema="example.summary.v1"
        ) == {
            key: CacheHit(record={"summary": "hello"}),
            "missing": None,
        }


asyncio.run(main())
```

`CanonicalJsonFile` publishes one standalone document in an existing directory.
The caller must declare the maximum accepted canonical byte length:

```python
from pathlib import Path

from dr_store import CanonicalJsonFile

artifact_directory = Path("artifacts")
artifact_directory.mkdir(exist_ok=True)
metadata = CanonicalJsonFile(
    artifact_directory,
    "metadata.json",
    max_bytes=1 << 20,
)
metadata.publish({"state": "complete"})
assert metadata.read() == {"state": "complete"}
```

Use the lower-level `await SqliteBackend.open(path)` when assembling an
`ObjectStore` directly whose objects and bindings must persist across processes.
Close it with `await backend.aclose()` or an async context. The rendered
[definitions](https://danielle-rothermel.github.io/dr-store/), authoritative
[terms](https://github.com/danielle-rothermel/dr-store/blob/main/.defs/terms.toml),
and binding
[contracts](https://github.com/danielle-rothermel/dr-store/blob/main/.defs/contracts.toml)
describe the vocabulary, public-export mappings, and behavioral boundaries.

## Content addressing

[Content addressing](https://github.com/danielle-rothermel/dr-store/blob/main/src/dr_store/content_addressing.py)
validates each schema-qualified reference and derives its content hash through
dr-serialize's canonical JSON profile. Its stable public shape is:

```python
@dataclass(frozen=True, slots=True)
class ObjectReference:
    schema: str
    content_hash: str

    @classmethod
    def for_record(cls, schema: str, record: Jsonable) -> ObjectReference: ...
    def verify_record(self, record: Jsonable) -> None: ...

def compute_content_hash(record: Jsonable) -> str: ...
def is_content_hash(value: str) -> bool: ...

OBJECT_REFERENCE_PREFIX = "dr-store-object:v1"

def format_object_reference(reference: ObjectReference) -> str: ...
def parse_object_reference(value: str) -> ObjectReference: ...
```

``format_object_reference`` and ``parse_object_reference`` pin the opaque wire
string that platform consumers store as evidence or output references.

## Object Store

The [Object Store](https://github.com/danielle-rothermel/dr-store/blob/main/src/dr_store/object_store.py)
owns immutable record operations and opaque key bindings. Its statuses and
store surface keep storage outcomes distinct from stored records:

```python
class PutStatus(Enum):
    STORED = "stored"
    IDEMPOTENT = "idempotent"

class BindStatus(Enum):
    BOUND = "bound"
    IDEMPOTENT = "idempotent"

class EvictStatus(Enum):
    EVICTED = "evicted"
    ABSENT = "absent"

class ObjectStore:
    def __init__(self, backend: Backend) -> None: ...
    async def put(
        self, schema: str, record: Jsonable
    ) -> tuple[ObjectReference, PutStatus]: ...
    async def get(self, reference: ObjectReference) -> Jsonable: ...
    async def get_bound_objects(
        self, keys: Iterable[str]
    ) -> Mapping[str, BoundObjectRow]: ...
    async def get_many(
        self, keys: Iterable[str], *, schema: str
    ) -> dict[str, StoreHit | None]: ...
    async def put_many(
        self, entries: Mapping[str, tuple[str, Jsonable]]
    ) -> dict[str, ObjectReference]: ...
    def verify_stored_record(
        self,
        *,
        reference: ObjectReference,
        stored_schema: str,
        canonical: str,
    ) -> Jsonable: ...
    async def bind(
        self, key: str, reference: ObjectReference
    ) -> BindStatus: ...
    async def resolve(self, key: str) -> ObjectReference | None: ...
    async def evict_bindings(
        self, keys: Iterable[str]
    ) -> dict[str, EvictStatus]: ...
    def put_enlisted(
        self, connection: Connection, schema: str, record: Jsonable
    ) -> tuple[ObjectReference, PutStatus]: ...
    def bind_enlisted(
        self, connection: Connection, key: str, reference: ObjectReference
    ) -> BindStatus: ...
    def put_many_enlisted(
        self,
        connection: Connection,
        entries: Mapping[str, tuple[str, Jsonable]],
    ) -> dict[str, ObjectReference]: ...
    def get_bound_objects_enlisted(
        self, connection: Connection, keys: Iterable[str]
    ) -> Mapping[str, BoundObjectRow]: ...
    def get_enlisted(
        self, connection: Connection, reference: ObjectReference
    ) -> Jsonable: ...
    def get_many_enlisted(
        self, connection: Connection, keys: Iterable[str], *, schema: str
    ) -> dict[str, StoreHit | None]: ...
    def resolve_enlisted(
        self, connection: Connection, key: str
    ) -> ObjectReference | None: ...
```

`get_bound_objects` deduplicates requested keys and returns joined binding/object
rows without verification. `verify_stored_record` applies the same checks as
`get` to one stored row. `get_many` composes those steps for evidence-grade
bulk reads: deduplicated keys, one verified hit or unbound `None` for every
distinct key. A hit wraps the record so a bound strict-JSON `null` value is
distinct from an unbound key. Wrong binding schemas, missing referenced
objects, and unverifiable stored content raise typed errors rather than
reporting cache-style misses. `put_many` validates, canonicalizes, and hashes
every proposed entry before one backend write batch, then returns the first
binding winner for each input key. A batch read claims no single snapshot
across backend read chunks.

`evict_bindings` is cache-grade: it deletes the binding rows for exact keys so
memoized entries become bustable, reports `ABSENT` instead of raising for
unbound keys, and never touches object rows, so evicted content stays
retrievable by reference. Evidence keys are never evicted — a requeued run
takes new keys — and there is deliberately no enlisted variant, so eviction
cannot be reached from inside an evidence transaction.

Verified object reads raise `ContentHashMismatchError` with
`ContentMismatchReason`. `actual` carries the observed hash only for
`HASH_MISMATCH`; other reasons leave `actual` unset.

## Storage backends

[Storage backends](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/storage_backends)
implement one atomic protocol beneath the Object Store. Outcome objects carry
the existing row when an append-only operation does not insert. Batch value
objects carry prepared writes and joined binding/object read results:

```python
@dataclass(frozen=True, slots=True)
class PutOutcome:
    inserted: bool
    stored_canonical: str

@dataclass(frozen=True, slots=True)
class BindOutcome:
    bound: bool
    existing_schema: str
    existing_content_hash: str

@dataclass(frozen=True, slots=True)
class BoundObjectWrite:
    key: str
    schema: str
    content_hash: str
    canonical: str

@dataclass(frozen=True, slots=True)
class BoundObjectRow:
    binding_schema: str
    binding_content_hash: str
    canonical: str | None
```

```python
class Backend(Protocol):
    async def put_object(
        self, *, schema: str, content_hash: str, canonical: str
    ) -> PutOutcome: ...
    async def get_object(
        self, *, schema: str, content_hash: str
    ) -> tuple[str, str] | None: ...
    async def bind(
        self, *, key: str, schema: str, content_hash: str
    ) -> BindOutcome: ...
    async def get_binding(self, *, key: str) -> tuple[str, str] | None: ...
    async def get_bound_objects(
        self, *, keys: tuple[str, ...]
    ) -> dict[str, BoundObjectRow]: ...
    async def put_bound_objects(
        self, *, entries: tuple[BoundObjectWrite, ...]
    ) -> dict[str, BindOutcome]: ...
    async def delete_bindings(self, *, keys: tuple[str, ...]) -> set[str]: ...

class MemoryBackend: ...
class PostgresBackend:
    @classmethod
    def open_sync(
        cls,
        engine: Engine,
        *,
        batch_chunk_size: int = 512,
    ) -> PostgresBackend: ...
    @classmethod
    async def open(
        cls,
        engine: AsyncEngine,
        *,
        batch_chunk_size: int = 512,
    ) -> PostgresBackend: ...
    async def put_object(
        self, *, schema: str, content_hash: str, canonical: str
    ) -> PutOutcome: ...
    def put_object_enlisted(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
        connection: Connection,
    ) -> PutOutcome: ...
    async def bind(
        self, *, key: str, schema: str, content_hash: str
    ) -> BindOutcome: ...
    def bind_enlisted(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
        connection: Connection,
    ) -> BindOutcome: ...
    async def put_bound_objects(
        self, *, entries: tuple[BoundObjectWrite, ...]
    ) -> dict[str, BindOutcome]: ...
    def put_bound_objects_enlisted(
        self,
        *,
        entries: tuple[BoundObjectWrite, ...],
        connection: Connection,
    ) -> dict[str, BindOutcome]: ...
    async def get_object(
        self, *, schema: str, content_hash: str
    ) -> tuple[str, str] | None: ...
    def get_object_enlisted(
        self,
        *,
        schema: str,
        content_hash: str,
        connection: Connection,
    ) -> tuple[str, str] | None: ...
    async def get_binding(self, *, key: str) -> tuple[str, str] | None: ...
    def get_binding_enlisted(
        self, *, key: str, connection: Connection
    ) -> tuple[str, str] | None: ...
    async def get_bound_objects(
        self, *, keys: tuple[str, ...]
    ) -> dict[str, BoundObjectRow]: ...
    def get_bound_objects_enlisted(
        self, *, keys: tuple[str, ...], connection: Connection
    ) -> dict[str, BoundObjectRow]: ...

class SqliteBackend:
    @classmethod
    async def open(
        cls, path: str | Path, *, busy_timeout_ms: int = 30_000
    ) -> SqliteBackend: ...
    async def aclose(self) -> None: ...
```

Batch reads address only the supplied exact keys. SQLite performs chunked
joined binding/object queries and does not promise one snapshot across the
chunks. Every non-empty SQLite write batch uses one immediate transaction; a
failure rolls back that transaction. Committed rows persist across reopen, but
the backend does not promise power-loss durability.

PostgreSQL batches deduplicate objects and keys, use bounded set-based
statements, and fetch bindings separately from distinct referenced objects.
Every non-empty PostgreSQL write batch uses one transaction. The backend owns
neither installation nor engine lifecycle, validates the fixed schema format
during awaited open, and uses the fixed `dr_store` namespace regardless of the
connection's search path.

## Record Cache

The [Record Cache](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/record_cache)
is a memoization facade over an existing `ObjectStore`. It accepts opaque
caller-owned keys, with `derive_cache_key` as the canonical helper for
content-derived memoization. A typed hit keeps every strict JSON record,
including null, distinct from a miss. `SqliteRecordCache` supplies the managed
persistent form:

```python
def derive_cache_key(namespace: str, payload: Jsonable) -> str: ...

@dataclass(frozen=True, slots=True)
class CacheHit:
    record: Jsonable

@dataclass(frozen=True, slots=True)
class CacheEntry:
    schema: str
    record: Jsonable

@dataclass(frozen=True, slots=True)
class RecordCacheStats:
    corruption_count: int

class RecordCache:
    def __init__(self, store: ObjectStore) -> None: ...
    @property
    def stats(self) -> RecordCacheStats: ...
    async def get(self, key: str, *, schema: str) -> CacheHit | None: ...
    async def get_many(
        self, keys: Iterable[str], *, schema: str
    ) -> dict[str, CacheHit | None]: ...
    async def put(
        self, key: str, schema: str, record: Jsonable
    ) -> ObjectReference: ...
    async def put_many(
        self, entries: Mapping[str, CacheEntry]
    ) -> dict[str, ObjectReference]: ...

class SqliteRecordCache(RecordCache):
    @classmethod
    async def open(
        cls, path: str | Path, *, busy_timeout_ms: int = 30_000
    ) -> SqliteRecordCache: ...
    async def aclose(self) -> None: ...
```

`get_many` deduplicates requested keys and returns exactly those distinct keys,
with each value independently classified as a hit or miss under the requested
schema. It parses and verifies each returned bound object once; it does not
promise that a multi-chunk backend read observes one snapshot. `put_many`
validates, canonicalizes, and hashes each proposed entry once before invoking
one backend write batch, then returns the first binding winner for every input
key. Single-key `get` and `put` use the same paths and semantics.

The cache intentionally provides no scheduler, dirty tracking, key enumeration,
prefix query, delete, expiry, eviction, or size cap. Callers own those policies
and choose new keys for invalidation.

Opening captures a non-transient absolute filesystem path and establishes the
SQLite schema before returning; empty and `:memory:` paths are rejected. Each
instance owns one connection-affine worker and connection. Closing rejects new
cache operations, waits for every admitted `get`, `get_many`, `put`, or
`put_many` to finish, and then closes those owned resources. A successful close
is idempotent for repeated and concurrent callers.
`SqliteRecordCacheClosedError` reports
operations requested after closing begins, before their inputs are validated;
`SqliteRecordCacheCloseError` reports a terminal cleanup failure to close
callers, including a context exit. Cancelling one close waiter does not cancel
the shared terminal cleanup. Committed records remain available after close and
reopen. Closing one cache
does not close a separate instance or coordinate another process, even when
both use the same database path. These persistence semantics do not promise
power-loss durability.

Unverifiable stored cache values still report a miss, but increment
`RecordCache.stats.corruption_count` and log the corrupted key.

## Canonical JSON document files

A [canonical JSON document file](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/document_file)
owns publication and verified reads for one caller-named document in an existing
directory. The byte bound is required; the nesting-depth bound defaults to the
dr-serialize canonical profile maximum and applies to both publication and
read:

```python
class CanonicalJsonFile:
    def __init__(
        self,
        directory: str | Path,
        name: str,
        *,
        max_bytes: int,
        max_depth: int = CANONICAL_JSON_MAX_CONTAINER_DEPTH,
    ) -> None: ...

    @property
    def path(self) -> Path: ...
    def publish(self, document: Jsonable) -> None: ...
    def read(self) -> Jsonable: ...
```

```python
@verify(UNIQUE)
class PublicationStage(StrEnum):
    ENCODE = "encode"
    CREATE_TEMP = "create_temp"
    WRITE_TEMP = "write_temp"
    REPLACE_TARGET = "replace_target"

@verify(UNIQUE)
class ReadStage(StrEnum):
    OPEN_DIRECTORY = "open_directory"
    OPEN_CHILD = "open_child"
    READ_BYTES = "read_bytes"
    DECODE = "decode"
    VERIFY_CANONICALITY = "verify_canonicality"

@verify(UNIQUE)
class RegularChildFailureReason(StrEnum):
    MISSING = "missing"
    NOT_REGULAR = "not_regular"
    MISMATCH = "mismatch"
    BOUNDS_EXCEEDED = "bounds_exceeded"

@verify(UNIQUE)
class ReplacementState(StrEnum):
    NOT_REPLACED = "not_replaced"
    REPLACED = "replaced"
    UNKNOWN = "unknown"
```

`DocumentPublishError` reports a `PublicationStage` and `ReplacementState`.
`NOT_REPLACED` means replacement was not invoked and any prior target remains
authoritative. `REPLACED` means replacement returned before later finalization
failed. `UNKNOWN` means the replacement operation itself failed and cannot
prove whether the target changed, so callers must inspect or coordinate before
treating either value as authoritative. `DocumentReadError` reports
`ReadStage`, `RegularChildFailureReason`, and the requested path. `MISSING` means the selected
document is absent; every other reason means the document is present but
invalid. Both errors derive from `DocumentFileError` and preserve the
originating failure as their cause. `ManifestPublishError` and
`ManifestReadError` expose the same structured fields on the directory surface.

## Document Directory

A [Document Directory](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/document_directory)
groups one canonical JSON Manifest with streamed binary Sidecars. The directory
owns allocation and publication while `SidecarWriter` owns bounded retention:

```python
class DocumentDirectory:
    def __init__(
        self,
        path: Path,
        manifest_name: str,
        *,
        manifest_max_bytes: int,
        manifest_max_depth: int = CANONICAL_JSON_MAX_CONTAINER_DEPTH,
    ) -> None: ...

    @classmethod
    def allocate(
        cls,
        root: str | Path,
        *,
        prefix: str,
        manifest_name: str,
        manifest_max_bytes: int,
        manifest_max_depth: int = CANONICAL_JSON_MAX_CONTAINER_DEPTH,
    ) -> DocumentDirectory: ...

    def publish(self, manifest: Jsonable) -> None: ...
    def open_sidecar(
        self,
        name: str,
        *,
        head_cap: int | None = None,
        tail_cap: int | None = None,
    ) -> SidecarWriter: ...

    def read_manifest(self) -> Jsonable: ...

    def verify_sidecar(
        self,
        name: str,
        *,
        expected_sidecar_hash: str,
        expected_head_length: int,
        expected_tail_length: int,
    ) -> None: ...
```

```python
@dataclass(frozen=True, slots=True)
class SidecarSummary:
    head_length: int
    tail_length: int
    produced: int
    dropped: int
    sidecar_hash: str

class SidecarWriter:
    def write(self, chunk: bytes) -> None: ...
    def finalize(self) -> SidecarSummary: ...
```

## Filesystem and failure semantics

Canonical document publication creates a reserved unique temporary file with
private permissions for each call, writes its complete canonical bytes, closes
it, replaces the target in the same directory, and closes the directory. The
path performs no `F_FULLFSYNC`, `fsync`, or directory flush and defines no
configurable durability mode. Successful publication is process-visible and
atomic to readers, without promising survival across machine or power loss. The
case-insensitive `.dr-store-document-` prefix is reserved for these temporary
files and cannot be used by document targets or Document Directory sidecars.
Concurrent supported publishers use independent temporary files, and the last
successful replacement is authoritative. Publication does not provide locks,
compare-and-set, multi-file transactions, or ordering with Sidecar writes.

All-or-nothing visibility depends on the underlying filesystem honoring atomic
same-directory replacement; network, synchronized, or other filesystems whose
rename semantics are not established are outside current evidence. A final
directory close failure raises even though replacement may already be visible
and does not roll the document back. Document Directory allocation uses a
timestamp and UUID4, but a generated-name collision raises `AllocationError`
rather than being retried, and allocation does not flush the caller-owned root
directory.

Canonical files, Document Directories, and persistent SQLite storage capture a
lexical absolute path at construction, so later working-directory changes do
not redirect their operations. This does not freeze symlink targets.

Canonical document reads open the named directory and regular direct child with
required no-follow, directory-relative flags, then stream from the child
descriptor they inspected through shared internals in
[`core/filesystem.py`](src/dr_store/core/filesystem.py). They read only to the
configured byte bound plus the single byte needed to detect overflow; byte-bound
failures report `ReadStage.READ_BYTES`. They enforce the configured
nesting-depth bound at decode, and require one complete UTF-8 strict JSON value
whose bytes are exactly canonical. Final-component symlinks and non-regular
files are rejected, a replacement after open cannot redirect that read to a
different inode, and platforms without the required descriptor operations fail
closed.

Outside the reserved publication namespace, [`validate_safe_name`](src/dr_store/core/filesystem.py)
prevents lexical traversal syntax across canonical files, document directories,
and verified regular-child reads. Sidecar creation and writes follow existing
final-component symlinks and therefore require trusted, caller-controlled
directory contents.
Sidecar writer coordination remains the caller's concern. Sidecar finalization
flushes userspace buffers and closes the Sidecar descriptor before returning
its stored-byte accounting and sidecar hash, but it does not flush the
Sidecar's directory entry or impose ordering on document publication.
Sidecar verification also refuses final-component symlinks for both the
Document Directory and named child, requires a regular direct child, and reads
from the descriptor it inspected. Failures raise `SidecarVerificationError`
with `RegularChildFailureReason` (`MISSING`, `NOT_REGULAR`, `MISMATCH`,
`BOUNDS_EXCEEDED`, or `UNSUPPORTED_PLATFORM`). Shared pinned-read internals in
[`core/filesystem.py`](src/dr_store/core/filesystem.py) and
[`descriptor_io.py`](src/dr_store/core/descriptor_io.py) back both canonical
document reads and `read_verified_regular_child`, which verifies a
caller-supplied byte length and SHA-256 digest and returns the verified bytes.
`DocumentDirectory.verify_sidecar` delegates to it without returning bytes.

A failed Sidecar `write` raises `AllocationError` and may leave its descriptor
open and its accounting state advanced. The writer is unusable by contract and
must be abandoned; retrying it or finalizing it has no supported outcome.
