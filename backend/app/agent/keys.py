"""The Idempotency-Key for agent tool calls is DERIVED BY THE BACKEND, never taken from the model.

    agent:<conversation_id>:<message_id>:<tool>:<hash of the canonical arguments>

* `conversation_id` and `message_id` come from the authenticated request (message_id is generated
  by the client and is unique per user), so the same chat message always yields the same keys:
  if the request is retried, or the model proposes the same call twice in one turn, the executor
  REPLAYS the first result instead of acting twice.
* Two DIFFERENT message ids give different keys, so a second message asking for "the same"
  refund is evaluated by policy on its own merits (remaining refundable amount, velocity).
* The canonical argument hash makes `"250"` and `"250.00"` the same call.
* Keys are scoped per user by the database, fit the 8-255 character rule, and use only the
  allowed charset. A model-supplied `idempotency_key` argument is rejected by the tool schemas.
"""

import hashlib
import json
import re
import uuid
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.modules.refunds.hashing import canonical_amount


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return canonical_amount(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return value


def canonical_args(args: dict[str, Any] | BaseModel) -> str:
    """Stable JSON text of the arguments. A validated model is preferred (it normalises types);
    raw dicts (invalid or unknown calls) are hashed as they are."""
    data = args.model_dump() if isinstance(args, BaseModel) else args
    return json.dumps(_canonical(data), sort_keys=True, separators=(",", ":"), default=str)


def derive_idempotency_key(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    tool: str,
    args: dict[str, Any] | BaseModel,
) -> str:
    digest = hashlib.sha256(canonical_args(args).encode("utf-8")).hexdigest()[:20]
    safe_tool = re.sub(r"[^A-Za-z0-9_]", "_", tool)[:40]  # the model chose this string
    return f"agent:{conversation_id}:{message_id}:{safe_tool}:{digest}"
