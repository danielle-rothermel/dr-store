from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dr_store.core.reasons import RegularChildFailureReason


class VerifiedRegularChildReadError(Exception):
    """Failed to read or verify one pinned regular direct child."""

    def __init__(
        self,
        message: str,
        *,
        reason: RegularChildFailureReason,
    ) -> None:
        self.reason = reason
        super().__init__(message)
