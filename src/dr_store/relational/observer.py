from __future__ import annotations

from typing import Protocol


class TransactionObserver(Protocol):
    def transaction_attempted(self) -> None: ...

    def transaction_acquired(self) -> None: ...
