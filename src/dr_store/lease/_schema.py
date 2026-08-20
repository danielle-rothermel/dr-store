from __future__ import annotations

from typing import NoReturn

from dr_store.lease.models import LeaseAuthoritySchemaMismatchError
from dr_store.relational.errors import (
    RelationalContractMismatchError,  # noqa: TC001
)


def reraise_schema_mismatch(exc: RelationalContractMismatchError) -> NoReturn:
    raise LeaseAuthoritySchemaMismatchError(
        table=exc.table,
        aspect=exc.aspect,
        expected=exc.expected,
        actual=exc.actual,
    ) from exc
