"""Contract between the executor and individual tools.

A tool handler has two phases so the executor can run a separate, auditable POLICY stage:

* `decide`  - load the facts the policy needs (taking row locks when the tool mutates), run
              the pure policy engine, return its verdict. No writes.
* `execute` - only called when the verdict is ALLOW or REQUIRE_APPROVAL. Does the database
              writes (and outbox rows) in the SAME transaction as `decide`, so the locks taken
              while deciding still protect the writes.

Anything with external side effects (e.g. paying out money) goes in `after_commit`, which the
executor runs only after the transaction has committed.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import ToolCallStatus
from app.policy.rules import Rules
from app.policy.types import PolicyResult
from app.tools.context import ToolContext


class ToolError(Exception):
    """A safe, expected failure (not found, conflict, ...). Rolls the transaction back, is
    logged on the tool call, and its message is safe to return to the caller."""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(slots=True)
class Decision:
    policy: PolicyResult
    state: Any = None  # whatever `decide` loaded and `execute` needs (e.g. the locked order)


@dataclass(slots=True)
class AfterCommitResult:
    status: ToolCallStatus
    data: dict[str, Any] = field(default_factory=dict)  # merged over ToolResult.data
    error_code: str | None = None
    error: str | None = None


AfterCommit = Callable[[AsyncSession], Awaitable[AfterCommitResult]]


@dataclass(slots=True)
class ToolResult:
    data: dict[str, Any]
    http_status: int = 200
    after_commit: AfterCommit | None = None


class ToolHandler(Protocol):
    async def decide(
        self, session: AsyncSession, ctx: ToolContext, args: Any, rules: Rules
    ) -> Decision: ...

    async def execute(
        self, session: AsyncSession, ctx: ToolContext, args: Any, decision: Decision
    ) -> ToolResult: ...
