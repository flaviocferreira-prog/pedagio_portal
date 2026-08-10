from __future__ import annotations

import logging
import re
from collections.abc import Iterable


LOGGER_NAME = "conferencia"
REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = (
    "authorization",
    "api[_-]?key",
    "assinatura(?:_recibo)?",
    "cookie",
    "code",
    "credential(?:s)?",
    "nonce",
    "password",
    "sync[_-]?shared[_-]?secret",
    "shared[_-]?secret",
    "secret",
    "senha",
    "state",
    "token",
)
_KEY_PATTERN = "(?:" + "|".join(_SENSITIVE_KEYS) + ")"
_QUERY_VALUE = re.compile(
    rf"(?i)([?&;]\s*{_KEY_PATTERN}\s*=)([^&#\s]*)"
)
_KEY_VALUE = re.compile(
    rf"(?i)(\b{_KEY_PATTERN}\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^,\s}}]+)"
)
_AUTHORIZATION = re.compile(r"(?i)(\bauthorization\s*:\s*)([^\r\n]+)")


def redact_for_log(value: object) -> str:
    """Keep diagnostic context while never emitting credentials or URL tokens."""
    text = str(value)
    text = _QUERY_VALUE.sub(rf"\1{REDACTED}", text)
    text = _AUTHORIZATION.sub(rf"\1{REDACTED}", text)
    return _KEY_VALUE.sub(rf"\1{REDACTED}", text)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def log_http_access(message: str, arguments: Iterable[object]) -> None:
    try:
        rendered = message % tuple(arguments)
    except (TypeError, ValueError):
        rendered = " ".join((message, *(str(argument) for argument in arguments)))
    logging.getLogger(LOGGER_NAME).info("http_access=%s", redact_for_log(rendered))


def log_sanitized_exception(operation: str, error: BaseException) -> None:
    logging.getLogger(LOGGER_NAME).error(
        "operation=%s error_type=%s error=%s",
        operation,
        type(error).__name__,
        redact_for_log(error),
    )
