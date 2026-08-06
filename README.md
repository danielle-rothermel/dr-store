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
  supply the Object Store's atomic, append-only operations. `MemoryBackend` is
  process-local; `SqliteBackend` persists data for cross-process use.
- **[Record Cache](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/record_cache)**
  memoizes records under opaque caller-owned keys. Reads return typed hits;
  absent, missing, or unverifiable stored values are misses, while invalid
  requested schemas and operational backend faults raise. Entries are never
  rebound, so callers invalidate by selecting a new key; `derive_cache_key`
  provides a canonical scheme using a versioned namespace and payload.
  `SqliteRecordCache(path)` is the managed persistent lifecycle.
- **[Canonical JSON document files](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/document_file)**
  publish and read one standalone, bounded canonical document in an existing
  directory through descriptor-pinned filesystem operations.
- **[Document Directory](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/document_directory)**
  delegates one bounded canonical Manifest to that file capability beside
  streamed binary Sidecars.

## Installation

dr-store requires Python 3.12 or newer.

```console
python -m pip install dr-store
```

## Usage

```python
from dr_store import MemoryBackend, ObjectStore

store = ObjectStore(MemoryBackend())
reference, _ = store.put("example.note.v1", {"title": "hello"})
store.bind("notes/latest", reference)

assert store.resolve("notes/latest") == reference
assert store.get(reference) == {"title": "hello"}
```

`SqliteRecordCache(path)` is the paved persistent Record Cache. It initializes
its database before returning and closes its current-process resources on
normal or exceptional context exit. When cleanup succeeds, an exception from
the context body is not suppressed; cleanup failure raises
`SqliteRecordCacheCloseError`:

```python
from dr_store import CacheHit, SqliteRecordCache, derive_cache_key

key = derive_cache_key("example.summary.v1", {"document": "note-42"})
with SqliteRecordCache("records.sqlite3") as cache:
    cache.put(key, "example.summary.v1", {"summary": "hello"})
    assert cache.get(key, schema="example.summary.v1") == CacheHit(
        record={"summary": "hello"}
    )
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

Use the lower-level `SqliteBackend(path)` when assembling an `ObjectStore`
directly whose objects and bindings must persist across processes. The rendered
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
    def put(
        self, schema: str, record: Jsonable
    ) -> tuple[ObjectReference, PutStatus]: ...
    def get(self, reference: ObjectReference) -> Jsonable: ...
    def bind(
        self, key: str, reference: ObjectReference
    ) -> BindStatus: ...
    def resolve(self, key: str) -> ObjectReference | None: ...
```

## Storage backends

[Storage backends](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/storage_backends)
implement one atomic protocol beneath the Object Store. Outcome objects carry
the existing row when an append-only operation does not insert:

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
```

```python
class Backend(Protocol):
    def put_object(
        self, *, schema: str, content_hash: str, canonical: str
    ) -> PutOutcome: ...
    def get_object(
        self, *, schema: str, content_hash: str
    ) -> tuple[str, str] | None: ...
    def bind(
        self, *, key: str, schema: str, content_hash: str
    ) -> BindOutcome: ...
    def get_binding(self, *, key: str) -> tuple[str, str] | None: ...

class MemoryBackend: ...
class SqliteBackend:
    def __init__(self, path: str | Path) -> None: ...
```

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

class RecordCache:
    def __init__(self, store: ObjectStore) -> None: ...
    def get(self, key: str, *, schema: str) -> CacheHit | None: ...
    def put(
        self, key: str, schema: str, record: Jsonable
    ) -> ObjectReference: ...

class SqliteRecordCache(RecordCache):
    def __init__(self, path: str | Path) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> SqliteRecordCache: ...
    def __exit__(self, ...) -> bool: ...
```

Construction establishes the SQLite schema before returning. Closing rejects
new cache operations, waits for every admitted `get` or `put` to finish, and
then closes all operational connections tracked by that cache instance in the
current process. A successful close is idempotent for repeated and concurrent
callers. `SqliteRecordCacheClosedError` reports operations requested after
closing begins, before their inputs are validated; `SqliteRecordCacheCloseError`
reports a terminal cleanup failure to every close caller, including a context
exit. Committed records remain available after close and reopen. Closing one
cache does not close a separate instance or coordinate another process, even
when both use the same database path. These persistence semantics do not
promise power-loss durability.

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

`DocumentPublishError` reports a `PublicationStage` and
`replacement_completed`. A false replacement value means that publication did
not replace the target and any prior target remains authoritative. A true value
means replacement occurred before later finalization failed. `DocumentReadError`
reports the requested path. Both derive from `DocumentFileError` and preserve
the originating failure as their cause. The reporting phases are `ENCODE`,
`CREATE_TEMP`, `WRITE_TEMP`, `FLUSH_TEMP`, `REPLACE_TARGET`, and
`FLUSH_DIRECTORY`; directory acquire and temporary-file open belong to
`CREATE_TEMP`, temporary-file flush and close belong to `FLUSH_TEMP`, and
directory flush and close belong to `FLUSH_DIRECTORY`.

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
        expected_digest: str,
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
    digest: str

class SidecarWriter:
    def write(self, chunk: bytes) -> None: ...
    def finalize(self) -> SidecarSummary: ...
```

## Filesystem and failure semantics

Canonical document publication creates a reserved unique temporary file for
each call, writes its complete canonical bytes, flushes and closes it, replaces
the target in the same directory, and flushes and closes the directory.
Concurrent supported publishers use independent temporary files, and the last
successful replacement is authoritative. Publication does not provide locks,
compare-and-set, multi-file transactions, or ordering with Sidecar writes.

All-or-nothing visibility depends on the underlying filesystem honoring atomic
same-directory replacement; network, synchronized, or other filesystems whose
rename semantics are not established are outside current evidence. A final
directory flush or close failure raises even though replacement may already be
visible and does not roll the document back. Publication and allocation make no
power-loss durability promise. Document Directory allocation uses a timestamp
and UUID4, but a generated-name collision raises `AllocationError` rather than
being retried, and allocation does not flush the caller-owned root directory.

Canonical document reads open the named directory and regular direct child with
required no-follow, directory-relative flags, then stream from the child
descriptor they inspected. They read only to the configured byte bound plus the
single byte needed to detect overflow, enforce the configured nesting-depth
bound, and require one complete UTF-8 strict JSON value whose bytes are exactly
canonical. Final-component symlinks and non-regular files are rejected, a
replacement after open cannot redirect that read to a different inode, and
platforms without the required descriptor operations fail closed.

Name validation prevents lexical traversal syntax only. Sidecar creation and
writes follow existing final-component symlinks and therefore require trusted,
caller-controlled directory contents. Sidecar writer coordination remains the
caller's concern. Sidecar finalization flushes the Sidecar descriptor before
returning its summary, but it does not flush the Sidecar's directory entry or
impose ordering on document publication. Sidecar verification also refuses
final-component symlinks for both the Document Directory and named child,
requires a regular direct child, and reads from the descriptor it inspected.

A failed Sidecar `write` raises `AllocationError` and may leave its descriptor
open and its accounting state advanced. The writer is unusable by contract and
must be abandoned; retrying it or finalizing it has no supported outcome.
