"""The race: an administrator decides while the model is still writing its final answer, i.e.
AFTER the approval exists and BEFORE the turn reaches WAITING_FOR_HUMAN. The decision's event finds
a workflow that is not waiting yet. The orchestrator closes the gap by looking once more right
after it enters WAITING_FOR_HUMAN (app/agent/orchestrator.py, `_respond`).

The window is made deterministic with a model whose SECOND call first lets the admin decide."""

import uuid

import pytest

from app.agent.llm.fake import ScriptedLLM, call, say
from app.db.models import Approval, Message, Refund, WorkflowRun
from app.db.models.enums import WorkflowStatus
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.policy.types import Actor
from app.tests.agent_helpers import all_rows, chat, make_orchestrator
from app.tests.worlds import World, make_world
from app.tools.context import ToolContext


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


class DecidesDuringTheSecondCall:
    """Plays a script, but before answering the second time an administrator decides."""

    name = "fake"

    def __init__(self, script: ScriptedLLM, decide) -> None:
        self.script, self.decide, self.calls_made = script, decide, 0

    async def complete(self, **kw):
        self.calls_made += 1
        if self.calls_made == 2:  # the tool ran; the approval exists; now the final answer
            await self.decide()
        return await self.script.complete(**kw)


def refund_script(world: World) -> ScriptedLLM:
    return ScriptedLLM(
        call(
            "request_refund",
            order_id=str(world.order),
            amount="700.00",
            reason_code="damaged",
        ),
        say("I have sent your request to our team."),
    )


async def raced_turn(db_session, world: World, provider, tool: str):
    holder = {}

    async def decide() -> None:
        (approval,) = await all_rows(db_session, Approval)
        ctx = ToolContext(
            actor=Actor(id=world.admin2.id, role=world.admin2.role), request_id="req-race"
        )
        await holder["orch"].executor.run(db_session, ctx, tool, {"approval_id": str(approval.id)})

    llm = DecidesDuringTheSecondCall(refund_script(world), decide)
    orch = make_orchestrator(llm, provider)
    holder["orch"] = orch
    r = (await chat(orch, db_session, world.alice, "refund 700 for the damaged item")).response
    return r, uuid.UUID(r["workflow_id"]), llm


async def test_an_approval_decided_during_the_final_answer_still_finishes_the_turn(
    db_session, world
) -> None:
    r, run_id, llm = await raced_turn(
        db_session, world, SimulatedPaymentProvider(), "approve_refund"
    )

    (run,) = await all_rows(db_session, WorkflowRun, WorkflowRun.id == run_id)
    assert (run.state, run.status) == ("DONE", WorkflowStatus.completed)  # not stranded waiting
    assert run.context["outcome"] == {"approval_status": "approved", "refund_status": "succeeded"}
    msgs = sorted(
        await all_rows(db_session, Message, Message.workflow_run_id == run_id), key=lambda m: m.seq
    )
    assert "approved and has been paid" in msgs[-1].content[0]["text"]
    assert llm.calls_made == 2  # finishing the turn did not ask the model anything
    assert r["status"] == "waiting_approval"  # the frozen reply is what the customer saw first


async def test_a_rejection_during_the_final_answer_finishes_the_turn_too(db_session, world) -> None:
    _, run_id, _ = await raced_turn(db_session, world, SimulatedPaymentProvider(), "reject_refund")
    (run,) = await all_rows(db_session, WorkflowRun, WorkflowRun.id == run_id)
    assert run.status == WorkflowStatus.completed
    assert run.context["outcome"] == {"approval_status": "rejected", "refund_status": "rejected"}


async def test_an_approved_but_unfinished_payout_during_the_final_answer_keeps_waiting(
    db_session, world
) -> None:
    provider = SimulatedPaymentProvider()
    provider.unavailable_next()  # the provider will not answer: the refund stays `processing`
    _, run_id, _ = await raced_turn(db_session, world, provider, "approve_refund")
    (run,) = await all_rows(db_session, WorkflowRun, WorkflowRun.id == run_id)
    assert (run.state, run.status) == ("WAITING_FOR_HUMAN", WorkflowStatus.waiting_approval)
    (refund,) = await all_rows(db_session, Refund)
    assert refund.status.value == "processing"


async def test_with_no_decision_the_turn_simply_waits(db_session, world) -> None:
    llm = refund_script(world)
    orch = make_orchestrator(llm, SimulatedPaymentProvider())
    r = (await chat(orch, db_session, world.alice, "refund 700 for the damaged item")).response
    (run,) = await all_rows(db_session, WorkflowRun, WorkflowRun.id == uuid.UUID(r["workflow_id"]))
    assert run.status == WorkflowStatus.waiting_approval  # nobody decided: it keeps waiting
    (approval,) = await all_rows(db_session, Approval)
    assert approval.status.value == "pending"
