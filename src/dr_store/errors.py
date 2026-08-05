"""Typed error taxonomy for the dr-store contract.

Every failure mode named in the storage contract is a distinct exception
type so callers can branch on outcome without string matching. Conflicts
(a different reference or different content at an existing key) are
first-class *outcomes*, not bugs, and carry the preserved existing value so
the caller can inspect the durable winner.

The Object Store taxonomy is rooted at :class:`StoreError` and the Document
Directory taxonomy at :class:`DocumentDirectoryError`; the two roots are
independent so callers can branch on one component without catching the
other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dr_store.references import ObjectReference


class StoreError(Exception):
    """Base for every dr-store contract failure."""


class ReferenceValidationError(StoreError):
    """An ObjectReference is structurally invalid.

    Raised when the declared schema is empty or the ``content_hash`` is not
    a 64-character lowercase hex SHA-256 hash.
    """


class ContentHashMismatchError(StoreError):
    """A record's recomputed Content Hash does not match its reference.

    Raised on verified get when stored content no longer hashes to the
    reference it is filed under -- corruption, including stored bytes that
    are unparseable or not in canonical form -- and by direct
    :meth:`ObjectReference.verify_record` checks.
    """

    def __init__(
        self,
        *,
        expected: str,
        actual: str,
        schema: str,
    ) -> None:
        self.expected = expected
        self.actual = actual
        self.schema = schema
        super().__init__(
            f"content hash mismatch for schema {schema!r}: "
            f"expected {expected}, recomputed {actual}"
        )


class SchemaMismatchError(StoreError):
    """Stored content is filed under a different schema than requested.

    Raised on verified get when the reference's schema does not match the
    schema recorded for the stored content at that content hash.
    """

    def __init__(self, *, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"schema mismatch: reference declares {expected!r}, "
            f"stored content is {actual!r}"
        )


class ObjectNotFoundError(StoreError):
    """No stored record resolves the given ObjectReference."""

    def __init__(self, *, reference: ObjectReference) -> None:
        self.reference = reference
        super().__init__(
            f"no object for schema {reference.schema!r} "
            f"content_hash {reference.content_hash}"
        )


class ObjectConflictError(StoreError):
    """Different content was put at an already-occupied content-hash key.

    A Content Hash collision that is not an identical-value replay: the
    existing stored value is preserved and never overwritten. In practice
    this signals a genuine SHA-256 collision or a nonconforming or poisoned
    backend; it is surfaced rather than silently accepted.
    """

    def __init__(self, *, schema: str, content_hash: str) -> None:
        self.schema = schema
        self.content_hash = content_hash
        super().__init__(
            f"different content already stored at schema {schema!r} "
            f"content_hash {content_hash}"
        )


class BindingConflictError(StoreError):
    """A key is already bound to a different ObjectReference.

    The existing binding (the durable winner) is preserved unchanged and
    exposed as ``existing`` so the caller can inspect it. There is no
    overwrite path.
    """

    def __init__(
        self,
        *,
        key: str,
        existing: ObjectReference,
        requested: ObjectReference,
    ) -> None:
        self.key = key
        self.existing = existing
        self.requested = requested
        super().__init__(
            f"key {key!r} already bound to "
            f"({existing.schema!r}, {existing.content_hash}); "
            f"refusing to rebind to "
            f"({requested.schema!r}, {requested.content_hash})"
        )


class DocumentDirectoryError(Exception):
    """Base for every Document Directory failure."""


class AllocationError(DocumentDirectoryError):
    """A Document Directory or one of its Sidecars faulted on the write side.

    Raised when a name the component creates is not a safe single segment,
    when the generated directory already exists (a collision is surfaced,
    never retried), when the underlying ``mkdir`` fails, and when opening,
    writing, or durably flushing a Sidecar file fails. The originating OS
    error is preserved as ``__cause__``.
    """


class ManifestPublishError(DocumentDirectoryError):
    """A Manifest could not be durably published.

    Raised when the payload is not strict finite JSON and when the
    temp-write, flush, atomic replace, or directory flush fails. The
    originating OS or validation error is preserved as ``__cause__``.
    """


class ManifestReadError(DocumentDirectoryError):
    """A Manifest could not be read back as a canonical strict-JSON value.

    Covers a Manifest name that is not a safe single segment, a missing
    file, unreadable bytes, malformed JSON, non-strict JSON (a non-finite
    token), and bytes that decode but are not in canonical form. The
    originating OS or decoding error is preserved as ``__cause__``.
    """


class SidecarVerificationError(DocumentDirectoryError):
    """Stored Sidecar bytes do not match the caller's expectations.

    Raised when a segment length or the Sidecar Digest disagrees with the
    expectation the caller extracted from its own Manifest, and when the
    Sidecar file cannot be read. The originating OS error, when there is
    one, is preserved as ``__cause__``.
    """
