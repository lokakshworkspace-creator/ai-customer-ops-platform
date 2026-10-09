"""Finishing a workflow that is WAITING_FOR_HUMAN.

A chat turn that created an approval ends `waiting_approval`. It is finished, with ONE TEMPLATED
message, by `finish_waiting`; the model is not called and no tool runs. Three callers use it:

* the resume CONSUMER (app/events/consumers/resume.py), the normal path: a `refund.*` event arrives,
  and the consumer finishes the turn that waits for that refund;
* the orchestrator, right after a turn enters WAITING_FOR_HUMAN (it closes the race where an admin
  decided between the tool call and the end of the turn: that event would have found no waiting
  workflow);
* `sync_waiting`, a READ-TIME FALLBACK called from `GET /workflows/{id}` and at the start of the
  next chat turn, so a stopped consumer cannot leave a customer stuck. Because it is a fallback it
  can also HIDE a dead consumer; the demo and the tests prove the consumer's own path by looking at
  the database before any GET.

WHEN A TURN IS DONE. `waiting_for` says what a waiting turn still waits for:

* "approval": a human has not decided;
* "payout": it was approved, but the refund is still `processing` (the payout has not finished, or
  its outcome is unknown and the reconcile job has not resolved it yet);
* None: it is final (rejected, expired, or the refund succeeded or failed).

Only a final turn is finished. An approved refund that is still `processing` stays waiting: the
`refund.completed` / `refund.failed` event (also emitted by the reconcile path) finishes it, so the
customer is told "paid", not just "being processed".

WHY IT IS SAFE TO RUN FROM A GET REQUEST (a GET that writes) AND FROM A CONSUMER:

* It is a single, idempotent transition. The workflow row is locked (`FOR UPDATE`) and its status
  is re-checked under the lock; only a workflow that is still `waiting_approval` is changed, and
  the status flip and the one message it adds commit in the SAME transaction. A second call (a
  duplicate event, a concurrent GET, the consumer racing the fallback) sees `completed` and does
  nothing, so exactly one message is added.
* It only reads ids that the BACKEND wrote into the checkpoint (never model output), and it checks
  that the refund belongs to the workflow's user before using it.
* It moves nothing but this workflow's bookkeeping: no money, no approval decision, no model call.
  The decision itself was made by an administrator through the approvals endpoints.
"""

import uuid
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import store
from app.agent.fsm import STATUS_OF, State, check_transition
from app.agent.llm.base import TextBlock
from app.db.models import Approval, Refund, WorkflowRun
from app.db.models.enums import ApprovalStatus, MessageRole, RefundStatus, WorkflowStatus

WaitingFor = Literal["approval", "payout"]


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


def waiting_for(approval: Approval | None, refund: Refund | None) -> WaitingFor | None:
    """What a waiting turn still waits for (see the module docstring). None means: final.

    An unusable checkpoint (no approval or refund to look at) counts as still waiting for approval:
    a turn is never finished on information the backend cannot verify."""
    if approval is None or refund is None:
        return "approval"
    if approval.status == ApprovalStatus.pending:
        return "approval"
    if approval.status == ApprovalStatus.approved and refund.status == RefundStatus.processing:
        return "payout"
    return None


async def _rows(session: AsyncSession, run: WorkflowRun) -> tuple[Approval | None, Refund | None]:
    """The approval and refund this run waits for, or (None, None) if the checkpoint does not name
    a refund that belongs to the run's user."""
    ctx: dict[str, Any] = dict(run.context or {})
    if not (ctx.get("approval_id") and ctx.get("refund_id")):
        return None, None
    approval = await session.get(Approval, uuid.UUID(ctx["approval_id"]), populate_existing=True)
    refund = await session.get(Refund, uuid.UUID(ctx["refund_id"]), populate_existing=True)
    if approval is None or refund is None or refund.requested_by != run.user_id:
        return None, None
    return approval, refund


async def waiting_for_run(session: AsyncSession, run: WorkflowRun) -> WaitingFor | None:
    """For the API: what this run waits for right now (None if it is not waiting at all)."""
    if run.status != WorkflowStatus.waiting_approval:
        return None
    approval, refund = await _rows(session, run)
    return waiting_for(approval, refund)


async def finish_in_transaction(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Finish the workflow if what it waits for is final. Returns True if it changed.

    Neither commits nor rolls back: the CALLER owns the transaction. The resume consumer needs that,
    because it runs inside the dedupe transaction (the `processed_events` claim and this effect must
    commit together, and a rollback here would also undo the claim)."""
    run = (
        await session.execute(
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None or run.status != WorkflowStatus.waiting_approval:
        return False  # already finished (or never waiting): nothing to do

    approval, refund = await _rows(session, run)
    if approval is None or refund is None or waiting_for(approval, refund) is not None:
        return False  # still waiting (a human or the payout), or an unusable checkpoint

    ctx: dict[str, Any] = dict(run.context or {})
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
    return True


async def finish_waiting(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """`finish_in_transaction` in a transaction of its own: commit if it changed, else release."""
    changed = await finish_in_transaction(session, run_id)
    if changed:
        await session.commit()
    else:
        await session.rollback()  # nothing to write: release the row lock
    return changed


async def sync_waiting(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """The read-time FALLBACK: same transition, called from a GET or the next chat turn."""
    return await finish_waiting(session, run_id)


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
