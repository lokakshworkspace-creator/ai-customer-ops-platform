"""JSON helpers for storing arbitrary tool data in JSONB columns safely."""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")  # exact, never a float
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    if hasattr(value, "value"):  # enums
        return value.value
    return str(value)


def to_json_safe(value: Any) -> Any:
    """Round-trip through JSON so the result only contains str/int/float/bool/None/list/dict."""
    return json.loads(json.dumps(value, default=_default))


def cap_json(value: Any, max_bytes: int, *, preview_chars: int = 1000) -> Any:
    """JSON-safe copy of `value`, or a small marker if its serialised form exceeds `max_bytes`.

    The marker keeps the row useful for debugging (size and a preview) without letting a huge
    or hostile payload bloat the table. Matches the CHECK constraints on tool_calls.
    """
    safe = to_json_safe(value)
    text = json.dumps(safe, separators=(",", ":"))
    size = len(text.encode("utf-8"))
    if size <= max_bytes:
        return safe
    return {"_truncated": True, "_original_bytes": size, "_preview": text[:preview_chars]}
