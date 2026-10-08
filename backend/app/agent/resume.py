"""Resuming a workflow that is WAITING_FOR_HUMAN: a read-time stub for Phase 5.

Phase 3 has no event-driven resume. Instead, `sync_waiting` looks at the approval and the refund
the workflow is waiting for and, once a human has decided, finishes the workflow with a TEMPLATED
message. The LLM is not called and no tool runs. It is called from `GET /workflows/{id}` and at
the start of the next chat turn. Phase 5 replaces the polling with a consumer of the `refund.*`
events; the state machine (WAITING_FOR_HUMAN -> DONE) does not change.

WHY IT IS SAFE TO RUN FROM A GET REQUEST (a GET that writes):

* It is a single, idempotent transition. The workflow row is locked (`FOR UPDATE`) and its status
  is re-checked under the lock; only a workflow that is still `waiting_approval` is changed, and
  the status flip and the one message it adds commit in the SAME transaction. A second call (or a
  concurrent one, which waits on the lock) sees `completed` and does nothing, so repeated GETs add
  exactly one message.
* It only reads ids that the BACKEND wrote into the checkpoint (never model output), and it checks
  that the refund belongs to the workflow's user before using it.
* It moves nothing but this workflow's bookkeeping: no money, no approval decision, no model call.
  The decision itself was made by an administrator through the approvals endpoints.
"""

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import store
from app.agent.fsm import STATUS_OF, State, check_transition
from app.agent.llm.base import TextBlock
from app.db.models import Approval, Refund, WorkflowRun
from app.db.models.enums import ApprovalStatus, MessageRole, RefundStatus, WorkflowStatus


def outcome_text(approval: Approval, refund: Refund) -> str:
    amount = f"{refund.amount:.2f} {refund.currency}"
    if approval.status == ApprovalStatus.rejected:
        return f"Update: our team did not approve your refund of {amount}. It will not be paid."
    if approval.status == ApprovalStatus.expired:
        return f"Update: your refund request of {amount} expired without a decision."
    if refund.status == RefundStatus.succeeded:
        return f"Update: your refund of {amount} was approved and has been paid."
    if refund.status == RefundStatus.failed:
        return (
            f"Update: your refund of {amount} was approved, but the payout failed. "
            "Please contact support."
        )
    return f"Update: your refund of {amount} was approved; the payout is being processed."


async def sync_waiting(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Finish the workflow if its approval has been decided. Returns True if it changed."""
    run = (
        await session.execute(
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None or run.status != WorkflowStatus.waiting_approval:
        await session.rollback()  # already finished (or never waiting): nothing to do
        return False

    ctx: dict[str, Any] = dict(run.context or {})
    approval = refund = None
    if ctx.get("approval_id") and ctx.get("refund_id"):
        approval = await session.get(Approval, uuid.UUID(ctx["approval_id"]))
        refund = await session.get(Refund, uuid.UUID(ctx["refund_id"]))
    if (
        approval is None
        or refund is None
        or refund.requested_by != run.user_id
        or approval.status == ApprovalStatus.pending
    ):
        await session.rollback()  # still waiting for a human (or the checkpoint is unusable)
        return False

    text = outcome_text(approval, refund)
    check_transition(State.WAITING_FOR_HUMAN, State.DONE)
    ctx["outcome"] = {
        "approval_status": approval.status.value,
        "refund_status": refund.status.value,
    }
    store.add_message(
        session,
        conversation_id=run.conversation_id,
        user_id=run.user_id,
        run_id=run.id,
        role=MessageRole.assistant,
        blocks=[TextBlock(text)],
    )
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .values(
            state=State.DONE.value,
            status=STATUS_OF[State.DONE],
            context=ctx,
            completed_at=func.now(),
        )
    )
    await session.commit()
    return True


async def sync_conversation(session: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Called before a new turn so the model's history already contains the outcome."""
    ids = list(
        await session.scalars(
            select(WorkflowRun.id).where(
                WorkflowRun.conversation_id == conversation_id,
                WorkflowRun.status == WorkflowStatus.waiting_approval,
            )
        )
    )
    await session.rollback()  # end the read transaction before taking row locks
    for run_id in ids:
        await sync_waiting(session, run_id)
