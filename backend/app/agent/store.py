"""Persistence for the agent: the transcript (`messages`) and the checkpoint (`workflow_runs`).

Every function takes the caller's session and either only reads or leaves the commit to the
caller. The orchestrator never keeps ORM objects across a commit (they expire): it keeps ids and
plain dicts, and reads again when it needs to.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.fsm import STATUS_OF, State
from app.agent.llm.base import (
    Block,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.agent.llm.base import Message as LLMMessage
from app.db.models import Message as MessageRow
from app.db.models import ToolCall, WorkflowRun
from app.db.models.enums import MessageRole, WorkflowStatus
from app.db.models.message import MAX_CONTENT_BYTES

# --- blocks <-> JSON ------------------------------------------------------------------------


def block_to_json(b: Block) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {
        "type": "tool_result",
        "tool_use_id": b.tool_use_id,
        "content": b.content,
        "is_error": b.is_error,
    }


def block_from_json(d: dict[str, Any]) -> Block:
    if d["type"] == "text":
        return TextBlock(d["text"])
    if d["type"] == "tool_use":
        return ToolUseBlock(d["id"], d["name"], d["input"])
    return ToolResultBlock(d["tool_use_id"], d["content"], bool(d.get("is_error", False)))


def _fit(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The CHECK constraint caps a message at MAX_CONTENT_BYTES; cut text blocks to fit."""
    import json

    def size() -> int:
        return len(json.dumps(blocks, separators=(",", ":")).encode("utf-8"))

    while size() > MAX_CONTENT_BYTES:
        longest = max(
            (b for b in blocks if b["type"] in {"text", "tool_result"}),
            key=lambda b: len(b.get("text", b.get("content", ""))),
            default=None,
        )
        if longest is None:  # pragma: no cover - tool_use inputs are small
            break
        key = "text" if longest["type"] == "text" else "content"
        longest[key] = longest[key][: max(0, len(longest[key]) // 2)] + " [cut]"
    return blocks


# --- transcript -----------------------------------------------------------------------------


def add_message(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    run_id: uuid.UUID | None,
    role: MessageRole,
    blocks: list[Block],
) -> None:
    session.add(
        MessageRow(
            conversation_id=conversation_id,
            user_id=user_id,
            workflow_run_id=run_id,
            role=role,
            content=_fit([block_to_json(b) for b in blocks]),
        )
    )


async def load_history(session: AsyncSession, conversation_id: uuid.UUID) -> list[LLMMessage]:
    rows = await session.execute(
        select(MessageRow.role, MessageRow.content)
        .where(MessageRow.conversation_id == conversation_id)
        .order_by(MessageRow.seq)
    )
    return [
        LLMMessage(role.value, tuple(block_from_json(b) for b in content)) for role, content in rows
    ]


async def conversation_owner(session: AsyncSession, conversation_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(MessageRow.user_id).where(MessageRow.conversation_id == conversation_id).limit(1)
    )


async def assistant_texts(session: AsyncSession, run_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        select(MessageRow.content)
        .where(MessageRow.workflow_run_id == run_id, MessageRow.role == MessageRole.assistant)
        .order_by(MessageRow.seq)
    )
    texts = []
    for (content,) in rows:
        text = "\n".join(b["text"] for b in content if b.get("type") == "text").strip()
        if text:
            texts.append(text)
    return texts


# --- workflow runs ---------------------------------------------------------------------------


async def find_run(session: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID):
    return (
        await session.execute(
            select(WorkflowRun)
            .where(WorkflowRun.user_id == user_id, WorkflowRun.message_id == message_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def get_run(session: AsyncSession, run_id: uuid.UUID, user_id: uuid.UUID | None = None):
    """Owner-scoped when `user_id` is given: someone else's run is simply not found."""
    stmt = select(WorkflowRun).where(WorkflowRun.id == run_id)
    if user_id is not None:
        stmt = stmt.where(WorkflowRun.user_id == user_id)
    return (
        await session.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()


async def recent_turns(session: AsyncSession, user_id: uuid.UUID, since: datetime) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(WorkflowRun)
            .where(
                WorkflowRun.user_id == user_id,
                WorkflowRun.message_id.is_not(None),
                WorkflowRun.created_at >= since,
            )
        )
        or 0
    )


async def conversation_tool_calls(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(ToolCall)
            .join(WorkflowRun, WorkflowRun.id == ToolCall.workflow_run_id)
            .where(WorkflowRun.conversation_id == conversation_id)
        )
        or 0
    )


async def run_has_tool_calls(session: AsyncSession, run_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(func.count()).select_from(ToolCall).where(ToolCall.workflow_run_id == run_id)
        )
    )


async def conversation_was_escalated(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(func.count())
            .select_from(WorkflowRun)
            .where(
                WorkflowRun.conversation_id == conversation_id,
                WorkflowRun.status == WorkflowStatus.escalated,
            )
        )
    )


async def abandon_stale_runs(
    session: AsyncSession, conversation_id: uuid.UUID, stale_after_s: float
) -> None:
    """A turn that has been `running` for longer than the turn deadline (plus slack) died with its
    process. Mark it failed so it cannot block the conversation (one running turn per conversation
    is a unique index)."""
    await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.conversation_id == conversation_id,
            WorkflowRun.status == WorkflowStatus.running,
            WorkflowRun.updated_at
            < func.now() - func.make_interval(0, 0, 0, 0, 0, 0, stale_after_s),
        )
        .values(
            status=WorkflowStatus.failed,
            state=State.FAILED.value,
            error="abandoned: no activity",
        )
    )


def new_run(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    context: dict[str, Any],
) -> uuid.UUID:
    run = WorkflowRun(
        id=uuid.uuid4(),  # known now, not at flush
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        status=STATUS_OF[State.PROPOSE],
        state=State.PROPOSE.value,
        context=context,
    )
    session.add(run)
    return run.id


async def checkpoint(
    session: AsyncSession,
    run_id: uuid.UUID,
    state: State,
    context: dict[str, Any],
    *,
    response: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Persist state + context (golden rule 4). Does not commit."""
    values: dict[str, Any] = {
        "state": state.value,
        "status": STATUS_OF[state],
        "context": context,
    }
    if response is not None:
        values["response"] = response
    if error is not None:
        values["error"] = error[:500]
    if state in {State.DONE, State.FAILED, State.ESCALATED}:
        values["completed_at"] = func.now()
    await session.execute(update(WorkflowRun).where(WorkflowRun.id == run_id).values(**values))


async def delete_turn(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Undo a turn in which nothing happened (the model could not be reached before any tool ran),
    so the client can send the same message_id again."""
    await session.execute(delete(MessageRow).where(MessageRow.workflow_run_id == run_id))
    await session.execute(delete(WorkflowRun).where(WorkflowRun.id == run_id))
