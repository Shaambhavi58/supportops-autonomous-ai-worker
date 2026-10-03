"""Structured logging that omits raw goals, credentials, and tool payloads."""

import json
import logging
import re
from typing import Any

logger = logging.getLogger("supportops")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|password|secret|token)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+"),
)


def redact_text(value: str) -> str:
    """Redact common credential assignments before persistence or display."""
    result = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            result = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)
        else:
            result = pattern.sub("Bearer [REDACTED]", result)
    return result


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if any(term in str(key).lower() for term in ("secret", "password", "api_key", "token"))
            else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def log_event(event: str, *, run_id: str | None = None, **safe_fields: Any) -> None:
    """Emit a compact event record; callers must never pass credentials."""
    record = {"event": event, **({"run_id": run_id} if run_id else {})}
    logger.info(json.dumps(sanitize(record | safe_fields), sort_keys=True, default=str))