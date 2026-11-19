"""Domain errors.

Each error carries a stable machine ``code`` and structured ``context``. The bot layer maps the code
to a localized message; logs get the code and context (never secrets).
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code: str = "domain_error"

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.context: dict[str, Any] = context

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, context={self.context!r})"
        )


class NotFound(DomainError):
    code = "not_found"


class Forbidden(DomainError):
    """The actor is not allowed to perform the action (wrong party, wrong role, banned...)."""

    code = "forbidden"


class InvalidTransition(DomainError):
    """The action is not valid in the deal's current status (also covers repeated button presses)."""

    code = "invalid_transition"


class VersionConflict(DomainError):
    """The deal terms changed after the actor looked at them."""

    code = "version_conflict"


class ValidationFailed(DomainError):
    code = "validation_failed"


class LimitExceeded(DomainError):
    code = "limit_exceeded"


class IdempotencyConflict(DomainError):
    """The same idempotency key was reused with a different payload."""

    code = "idempotency_conflict"


class LedgerError(DomainError):
    code = "ledger_error"


class AmountError(ValidationFailed):
    code = "invalid_amount"


class ProviderUnavailable(ValidationFailed):
    """The payment provider/network cannot take the request right now."""

    code = "provider_unavailable"
