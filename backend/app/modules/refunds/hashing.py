"""Idempotency helpers: key validation and the canonical request hash."""

import hashlib
import json
import re
import uuid
from decimal import Decimal

# Restricted charset keeps keys safe to log, store and put in headers. 8+ characters so a key
# like "1" cannot collide with itself across unrelated requests from the same user.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:\-]{8,255}$"
_KEY = re.compile(IDEMPOTENCY_KEY_PATTERN)
HASH_VERSION = 1


def is_valid_idempotency_key(key: str | None) -> bool:
    return bool(key) and _KEY.fullmatch(key) is not None  # fullmatch: "$" would allow a trailing \n


def canonical_amount(amount: Decimal) -> str:
    """500, 500.0 and 500.00 are the same amount and must hash the same."""
    return format(amount.normalize(), "f")


def canonical_note(note: str | None) -> str | None:
    note = (note or "").strip()
    return note or None


def request_hash(
    *, order_id: uuid.UUID, amount: Decimal, reason_code: str, note: str | None
) -> str:
    """SHA-256 over EVERY field of the refund request (and a version tag), in a canonical form.

    The idempotency key itself and the caller are deliberately not part of it: the key scopes
    the lookup (per user) and the hash decides "same request again" vs "key reused for something
    else".
    """
    canonical = {
        "v": HASH_VERSION,
        "order_id": str(order_id),
        "amount": canonical_amount(amount),
        "reason_code": reason_code,
        "note": canonical_note(note),
    }
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
