from __future__ import annotations

from typing import Protocol


class TransactionObserver(Protocol):
    """Optional hooks for relational transaction boundaries.

    Implementations may count attempted versus acquired transactions. Schema
    initialization and other non-transactional setup deliberately do not invoke
    these hooks.
    """

    def transaction_attempted(self) -> None: ...

    def transaction_acquired(self) -> None: ...
