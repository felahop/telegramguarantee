"""Structured logging (structlog) with secret redaction.

Redaction is key-based: any field whose name looks like a secret is replaced, recursively, before
rendering. Value-based heuristics are deliberately not used — a 64-hex private key and a tx hash
look identical, and hiding tx hashes would make incident analysis impossible. The real guarantee is
that secrets are ``SecretStr`` in config and never passed to loggers; this is the safety net.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from pydantic import SecretStr

REDACTED = "***"
_SECRET_KEY_RE = re.compile(
    r"(secret|token|password|passwd|private|priv_key|mnemonic|seed|api_?key|authorization|signature|cookie|totp)",
    re.IGNORECASE,
)
# Field names that match the pattern above but are public by nature.
_PUBLIC_KEYS = frozenset({"invite_token_len", "token_contract", "spend_id"})


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, Mapping):
        return {k: (REDACTED if _is_secret_key(k) else _redact(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact(v, depth + 1) for v in value)
    return value


def _is_secret_key(key: object) -> bool:
    return isinstance(key, str) and key not in _PUBLIC_KEYS and bool(_SECRET_KEY_RE.search(key))


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key == "event":
            continue
        if _is_secret_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    # Route stdlib loggers (aiogram, sqlalchemy, alembic) through the same level.
    logging.basicConfig(level=level.upper(), stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")
    # SQL echo can contain bound parameters (addresses, amounts) — keep it off in production.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
