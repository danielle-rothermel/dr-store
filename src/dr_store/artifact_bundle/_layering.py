from __future__ import annotations

from dr_store.core.errors import DocumentDirectoryError
from dr_store.document_file import DocumentFileError

# Errors raised by the storage primitives this package layers over. A bundle
# failure reports the first cause beneath every such wrapper so callers see the
# originating serialization or operating-system exception.
_LAYERING_ERRORS = (DocumentDirectoryError, DocumentFileError)


def underlying_cause(error: BaseException) -> BaseException:
    """Return the first cause beneath the layered storage-primitive errors."""
    cause: BaseException = error
    while isinstance(cause.__cause__, _LAYERING_ERRORS):
        cause = cause.__cause__
    return cause.__cause__ if cause.__cause__ is not None else error
