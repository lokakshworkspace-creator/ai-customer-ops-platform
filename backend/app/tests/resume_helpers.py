"""Helpers for the resume-consumer tests: a waiting chat turn made with COMMITTED sessions."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.agent.llm.fake import ObedientFakeLLM
from app.db.models import Approval, WorkflowRun
from app.events.envelope import EventEnvelope
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.policy.types import Actor
from app.tests.agent_helpers import all_rows, chat, make_orchestrator
from app.tests.conftest import CommittedDB
from app.tests.worlds import World
from app.tools.context import ToolContext


class Turn:
    def __init__(self, orch, run_id: uuid.UUID, approval_id: uuid.UUID, refund_id: uuid.UUID):
        self.orch, self.run_id = orch, run_id
        self.approval_id, self.refund_id = approval_id, refund_id


async def waiting_turn(
    db: CommittedDB,
    world: World,
    provider: SimulatedPaymentProvider,
    amount: str = "700",
    order: uuid.UUID | None = None,
) -> Turn:
    """A chat turn waiting for approval. `order` defaults to the 1000.00 order; a test that makes
    several waiting turns passes the 3000.00 one so they all fit under its total."""
    orch = make_orchestrator(ObedientFakeLLM(), provider)
    async with db.maker() as s:
        text = f"my order {order or world.order} arrived damaged, refund {amount}"
        r = (await chat(orch, s, world.alice, text)).response
        assert r["status"] == "waiting_approval"
        run_id = uuid.UUID(r["workflow_id"])
        approvals = await all_rows(s, Approval)
        ids = {a.id for a in approvals}
        # the approval and refund this turn created are the newest ones
        approval = max(approvals, key=lambda a: a.created_at)
        assert approval.id in ids
        return Turn(orch, run_id, approval.id, approval.refund_id)


async def decide(db: CommittedDB, turn: Turn, world: World, tool: str = "approve_refund"):
    ctx = ToolContext(
        actor=Actor(id=world.admin2.id, role=world.admin2.role), request_id="req-admin-resume"
    )
    async with db.maker() as s:
        return await turn.orch.executor.run(s, ctx, tool, {"approval_id": str(turn.approval_id)})


def event(
    event_type: str, refund_id: uuid.UUID, customer_id: uuid.UUID, *, status: str = "succeeded"
) -> EventEnvelope:
    """A refund event as the relay would publish it (ids and state only)."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        request_id="req-test",
        aggregate_type="refund",
        aggregate_id=str(refund_id),
        data={
            "refund_id": str(refund_id),
            "order_id": str(uuid.uuid4()),
            "customer_id": str(customer_id),
            "requested_by": str(customer_id),
            "amount": "700.00",
            "currency": "INR",
            "reason_code": "damaged",
            "status": status,
        },
    )


async def run_of(db: CommittedDB, run_id: uuid.UUID) -> WorkflowRun:
    async with db.maker() as s:
        (run,) = await all_rows(s, WorkflowRun, WorkflowRun.id == run_id)
        return run


async def n(db: CommittedDB, model) -> int:
    async with db.maker() as s:
        return await s.scalar(select(func.count()).select_from(model)) or 0
