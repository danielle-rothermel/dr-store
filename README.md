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
  supply the Object Store's atomic, append-only point and batch operations.
  `MemoryBackend` is process-local; `SqliteBackend` persists committed data for
  cross-process use; `PostgresBackend` shares committed data through a
  caller-owned SQLAlchemy asynchronous engine.
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

PostgreSQL 16 through 18 installations use SQLAlchemy async with psycopg and an
explicit, absent-only schema installation step. The caller creates and owns the
engine; dr-store neither accepts a DSN nor disposes the engine:

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
Alembic ownership. `install_postgres` is a one-time deployment operation that
creates the namespace, its tables, and the exact
`dr-store-postgresql-v1` schema-format marker in one transaction on a UTF-8
database. Repeating installation is an error.
`await PostgresBackend.open(engine)` validates that marker before returning a
backend for the same awaited point and batch operations as the other backends;
it acquires and releases connections without disposing the engine. PostgreSQL
backend methods accept an optional explicit SQLAlchemy Core connection so
evidence reads and writes can join a caller-owned transaction; when provided,
dr-store never commits that connection. Enlisted `get_bound_objects` observes
one caller-transaction snapshot. Opening never installs, alters, adopts, or
upgrades storage.

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
```

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

class ObjectStore:
    def __init__(self, backend: Backend) -> None: ...
    async def put(
        self, schema: str, record: Jsonable
    ) -> tuple[ObjectReference, PutStatus]: ...
    async def get(self, reference: ObjectReference) -> Jsonable: ...
    async def bind(
        self, key: str, reference: ObjectReference
    ) -> BindStatus: ...
    async def resolve(self, key: str) -> ObjectReference | None: ...
```

## Storage backends

[Storage backends](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/storage_backends)
implement one atomic protocol beneath the Object Store. Outcome objects carry
the existing row when an append-only operation does not insert. Batch value
objects carry prepared writes and joined binding/object read results:

```python
@dataclass(frozen=True, slots=True)
class PutOutcome:
    inserted: bool
    stored_schema: str
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
    object_schema: str | None
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

class MemoryBackend: ...
class PostgresBackend:
    @classmethod
    async def open(
        cls,
        engine: AsyncEngine,
        *,
        batch_chunk_size: int = 512,
    ) -> PostgresBackend: ...
    async def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
        connection: AsyncConnection | None = None,
    ) -> PutOutcome: ...
    async def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
        connection: AsyncConnection | None = None,
    ) -> BindOutcome: ...
    async def put_bound_objects(
        self,
        *,
        entries: tuple[BoundObjectWrite, ...],
        connection: AsyncConnection | None = None,
    ) -> dict[str, BindOutcome]: ...
    async def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
        connection: AsyncConnection | None = None,
    ) -> tuple[str, str] | None: ...
    async def get_binding(
        self,
        *,
        key: str,
        connection: AsyncConnection | None = None,
    ) -> tuple[str, str] | None: ...
    async def get_bound_objects(
        self,
        *,
        keys: tuple[str, ...],
        connection: AsyncConnection | None = None,
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
class ReadReason(StrEnum):
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
`ReadStage`, `ReadReason`, and the requested path. `MISSING` means the selected
document is absent; every other reason means the document is present but
invalid. Both errors derive from `DocumentFileError` and preserve the
originating failure as their cause. `ManifestPublishError` and
`ManifestReadError` expose the same structured fields on the directory surface.

Verified object reads raise `ContentHashMismatchError` with
`ContentMismatchReason`. `actual` carries the observed hash only for
`HASH_MISMATCH`; other reasons leave `actual` unset.

Unverifiable stored cache values still report a miss, but increment
`RecordCache.stats.corruption_count` and log the corrupted key.

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
descriptor they inspected. They read only to the configured byte bound plus the
single byte needed to detect overflow, enforce the configured nesting-depth
bound, and require one complete UTF-8 strict JSON value whose bytes are exactly
canonical. Final-component symlinks and non-regular files are rejected, a
replacement after open cannot redirect that read to a different inode, and
platforms without the required descriptor operations fail closed.

Outside the reserved publication namespace, name validation prevents lexical
traversal syntax. Sidecar creation and writes follow existing final-component
symlinks and therefore require trusted, caller-controlled directory contents.
Sidecar writer coordination remains the caller's concern. Sidecar finalization
flushes userspace buffers and closes the Sidecar descriptor before returning
its stored-byte accounting and sidecar hash, but it does not flush the
Sidecar's directory entry or impose ordering on document publication.
Sidecar verification also refuses final-component symlinks for both the
Document Directory and named child, requires a regular direct child, and reads
from the descriptor it inspected. Failures raise `SidecarVerificationError`
with `SidecarVerificationReason` (`MISSING`, `NOT_REGULAR`, `MISMATCH`,
`BOUNDS_EXCEEDED`, or `UNSUPPORTED_PLATFORM`).

A failed Sidecar `write` raises `AllocationError` and may leave its descriptor
open and its accounting state advanced. The writer is unusable by contract and
must be abandoned; retrying it or finalizing it has no supported outcome.
