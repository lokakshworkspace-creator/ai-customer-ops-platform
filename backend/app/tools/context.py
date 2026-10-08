import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from app.policy.types import Actor


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything about a call that does NOT come from the (possibly model-controlled) arguments.

    `actor` is the authenticated user. It is the only source of caller identity: tool arguments
    are never trusted for it (and a `user_id` argument is rejected by the input schemas).
    `idempotency_key` comes from the HTTP header, or (for the agent) is derived by the backend
    from the conversation, message and arguments; it is never taken from model-supplied arguments.

    `origin` says who is asking. "agent" means the LLM proposed this call: the executor then
    refuses every tool that is not `agent_visible`, whatever the actor's role (an admin chatting
    with the assistant does not give the assistant admin tools).
    """

    actor: Actor
    request_id: str | None = None
    workflow_run_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    tool_call_id: uuid.UUID | None = None  # set by the executor once the call is logged
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    origin: Literal["api", "agent"] = "api"
