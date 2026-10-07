import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.policy.types import Actor


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything about a call that does NOT come from the (possibly model-controlled) arguments.

    `actor` is the authenticated user. It is the only source of caller identity: tool arguments
    are never trusted for it (and a `user_id` argument is rejected by the input schemas).
    `idempotency_key` comes from the HTTP header (or, in Phase 3, is derived by the backend),
    never from model-supplied arguments.
    """

    actor: Actor
    request_id: str | None = None
    workflow_run_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
