"""Logging setup that refuses to keep calendar or bot secrets.

Ancestor logger filters do not run for propagated records, and handlers are
often attached later (uvicorn, pytest). Redaction therefore wraps
`LogRecord.getMessage`, which every formatter calls.
"""

import logging
import re

_QUERY_SECRET = re.compile(r"(?i)([?&](?:authtoken|access_token|token)=)([^&\s\"']+)")
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_INSTALLED = False


def redact_secrets(message: str) -> str:
    message = _QUERY_SECRET.sub(r"\1•••", message)
    return _TELEGRAM_TOKEN.sub("•••", message)


def configure_logging() -> None:
    global _INSTALLED
    if not _INSTALLED:
        original = logging.LogRecord.getMessage

        def get_message(self: logging.LogRecord) -> str:
            return redact_secrets(original(self))

        logging.LogRecord.getMessage = get_message  # type: ignore[method-assign]
        _INSTALLED = True
    for name in ("httpx", "httpcore", "sqlalchemy", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
