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
  memoizes records under derived cache keys. Reads return typed hits; absent,
  missing, or unverifiable stored values are misses, while invalid requested
  schemas and operational backend faults raise. Entries are never rebound, so
  callers invalidate by versioning their key namespace.
- **[Document Directory](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/document_directory)**
  publishes one canonical Manifest beside streamed binary Sidecars.

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

The same store can provide memoization without introducing another backend:

```python
from dr_store import CacheHit, RecordCache, derive_cache_key

cache = RecordCache(store)
key = derive_cache_key("example.summary.v1", {"document": "note-42"})
cache.put(key, "example.summary.v1", {"summary": "hello"})

assert cache.get(key, schema="example.summary.v1") == CacheHit(
    record={"summary": "hello"}
)
```

Use `SqliteBackend(path)` when the stored objects and bindings must persist
across processes. The rendered
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
is a memoization facade over an existing `ObjectStore`. A typed hit keeps every
strict JSON record, including null, distinct from a miss:

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
```

## Document Directory

A [Document Directory](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/document_directory)
groups one canonical JSON Manifest with streamed binary Sidecars. The directory
owns allocation and publication while `SidecarWriter` owns bounded retention:

```python
class DocumentDirectory:
    def __init__(self, path: Path, manifest_name: str) -> None: ...

    @classmethod
    def allocate(
        cls, root: str | Path, *, prefix: str, manifest_name: str
    ) -> DocumentDirectory: ...

    def publish(self, manifest: Jsonable) -> None: ...
    def open_sidecar(
        self,
        name: str,
        *,
        head_cap: int | None = None,
        tail_cap: int | None = None,
    ) -> SidecarWriter: ...

    @classmethod
    def read_manifest(
        cls, path: str | Path, *, manifest_name: str
    ) -> Jsonable: ...

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

A Document Directory is intended for caller-coordinated single-writer use;
dr-store does not enforce that policy with a lock. Allocation uses a timestamp
and UUID4, but a generated-name collision raises `AllocationError` rather than
being retried. Allocation does not flush the caller-owned root directory.

Manifest publication writes and flushes a temporary file, replaces the Manifest
in the same directory, and then flushes the Document Directory. All-or-nothing
visibility depends on the underlying filesystem honoring atomic same-directory
replacement; network, synchronized, or other filesystems whose rename semantics
are not established are outside current evidence. A failure of the final flush
raises even though the replacement may already be visible, and it does not roll
the Manifest back. These operations do not promise power-loss durability.

Name validation prevents lexical traversal syntax only. Manifest reads and
Sidecar creation and writes follow existing final-component symlinks, so those
paths require trusted, caller-controlled directory contents. Sidecar
finalization flushes the Sidecar descriptor before returning its summary, but it
does not flush the Sidecar's directory entry or impose ordering on an arbitrary
Manifest publication. Sidecar verification is the no-follow path: it refuses
final-component symlinks for both the Document Directory and named child,
requires a regular direct child, and reads from the descriptor it inspected.

A failed Sidecar `write` raises `AllocationError` and may leave its descriptor
open and its accounting state advanced. The writer is unusable by contract and
must be abandoned; retrying it or finalizing it has no supported outcome.
