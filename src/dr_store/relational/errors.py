from __future__ import annotations

from dr_store.core.store_errors import StoreError


class RelationalContractMismatchError(StoreError):
    """Owned table/metadata does not match the pinned contract."""

    def __init__(
        self,
        *,
        table: str,
        aspect: str,
        expected: object,
        actual: object,
    ) -> None:
        self.table = table
        self.aspect = aspect
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"incompatible relational table {table!r}: expected exact "
            f"{aspect} {expected!r}, found {actual!r}"
        )
