# dr-store

[![CI](https://github.com/danielle-rothermel/dr-store/actions/workflows/ci.yml/badge.svg)](https://github.com/danielle-rothermel/dr-store/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dr-store.svg)](https://pypi.org/project/dr-store/)

| [Repo Definitions](https://danielle-rothermel.github.io/dr-store/) | [dr-serialize v0.1.1](https://github.com/danielle-rothermel/dr-serialize) |
| --- | --- |

**dr-store provides domain-neutral storage primitives for immutable records and document artifacts with atomic Manifest publication.**
It is organized into these functional areas:

- **[Content addressing](https://github.com/danielle-rothermel/dr-store/blob/main/src/dr_store/content_addressing.py)**
  identifies complete records by their declared schemas and canonical-content
  hashes.
- **[Object Store](https://github.com/danielle-rothermel/dr-store/blob/main/src/dr_store/object_store.py)**
  provides immutable puts, verified reads, and atomic caller-owned key
  bindings.
- **[Storage backends](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/storage_backends)**
  provide interchangeable atomic storage operations.
- **[Document Directory](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/document_directory)**
  manages canonical manifests and streamed binary sidecars.
- **[Infrastructure](https://github.com/danielle-rothermel/dr-store/tree/main/src/dr_store/core)**
  supports the functional areas:
    - [Typed failures](https://github.com/danielle-rothermel/dr-store/blob/main/src/dr_store/core/errors.py)
      distinguish storage, publication, and verification failures.
    - [Filesystem support](https://github.com/danielle-rothermel/dr-store/blob/main/src/dr_store/core/filesystem.py)
      owns safe names and flush operations.

## Content Addressing

A complete record is addressed by its declared schema and the SHA-256 digest
of its Canonical JSON Text under dr-serialize's frozen profile. References
validate their own shape and can verify that a record still resolves to the
same content.

```python
CONTENT_HASH_LENGTH = 64


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

The Object Store owns immutable record storage, verified reads, and atomic
bindings from opaque caller-owned keys to object references. Repeating the
same write is idempotent; conflicting writes preserve the existing value.

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
        self,
        schema: str,
        record: Jsonable,
    ) -> tuple[ObjectReference, PutStatus]: ...

    def get(self, reference: ObjectReference) -> Jsonable: ...
    def bind(self, key: str, reference: ObjectReference) -> BindStatus: ...
    def resolve(self, key: str) -> ObjectReference | None: ...
```

## Storage Backends

The backend protocol defines the atomic storage operations shared beneath the
Object Store. Persistence and durability are implementation-specific:
`MemoryBackend` provides process-local storage, while `SqliteBackend` provides
durable cross-process storage.

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


class Backend(Protocol):
    def put_object(
        self,
        *,
        schema: str,
        content_hash: str,
        canonical: str,
    ) -> PutOutcome: ...

    def get_object(
        self,
        *,
        schema: str,
        content_hash: str,
    ) -> tuple[str, str] | None: ...

    def bind(
        self,
        *,
        key: str,
        schema: str,
        content_hash: str,
    ) -> BindOutcome: ...

    def get_binding(self, *, key: str) -> tuple[str, str] | None: ...
```

## Document Directory

A Document Directory holds one atomically published Manifest satisfying the
dr-serialize Canonical JSON Text profile and zero or more streamed binary
sidecars. The Manifest remains the caller's source of truth; the directory
owns publication, retention, and verification mechanics without interpreting
the payload. Sidecar verification accepts one safe direct-child name, refuses
final-component symlinks for both the Document Directory and named child,
requires the child to be a regular file, and streams bounded reads from the
same descriptor it inspected.

```python
class DocumentDirectory:
    @classmethod
    def allocate(
        cls,
        root: str | Path,
        *,
        prefix: str,
        manifest_name: str,
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
        cls,
        path: str | Path,
        *,
        manifest_name: str,
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

The filesystem operations have intentionally narrow persistence semantics.
Manifest publication flushes the temporary file, atomically replaces the
Manifest, and then flushes the directory. A failure of that last flush raises
even though the replacement may already be visible; it does not roll the
Manifest back. Allocation does not flush the caller-owned root directory, and
Sidecar finalization does not flush the Sidecar's directory entry. Abrupt
process-death visibility therefore does not establish power-loss durability.
On systems exposing `F_FULLFSYNC`, dr-store falls back to `fsync` after any
`F_FULLFSYNC` `OSError`, including errors that may not mean "unsupported."

A failed Sidecar `write` raises `AllocationError` but does not close or poison
the writer, and accounting may already include the failed chunk. Callers must
abandon that writer; retrying it or finalizing it has no supported outcome.
