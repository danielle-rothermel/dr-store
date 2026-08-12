from __future__ import annotations

from dr_store.core.reasons import RegularChildFailureReason  # noqa: TC001


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
