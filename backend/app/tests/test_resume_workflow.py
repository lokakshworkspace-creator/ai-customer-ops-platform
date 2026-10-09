"""Finishing a waiting chat turn (app/agent/resume.py): when it is final, and when it must keep
waiting. Real database, real executor and policy; the model is the fake and is never needed again
after the turn that created the approval."""

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agent import resume
from app.agent.llm.fake import ObedientFakeLLM
from app.api.deps import get_executor, get_orchestrator, get_session
from app.db.models import Approval, Message, OutboxEvent, Refund, ToolCall, WorkflowRun
from app.db.models.enums import ApprovalStatus, RefundStatus, WorkflowStatus
from app.main import app
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.modules.refunds.service import payout_flow
from app.policy.types import Actor
from app.tests.agent_helpers import all_rows, chat, count, make_orchestrator
from app.tests.tokens import auth_for
from app.tests.worlds import World, make_world
from app.tools.context import ToolContext


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


def ask_refund(world: World, amount: str) -> str:
    return f"my order {world.order} arrived damaged, refund {amount}"


async def waiting_turn(db_session, world: World, provider: SimulatedPaymentProvider):
    """A 700 refund: the turn ends waiting for a human. Returns (orch, run_id, approval_id)."""
    orch = make_orchestrator(ObedientFakeLLM(), provider)
    r = (await chat(orch, db_session, world.alice, ask_refund(world, "700"))).response
    assert r["status"] == "waiting_approval"
    (approval,) = await all_rows(db_session, Approval)
    return orch, uuid.UUID(r["workflow_id"]), approval.id


async def decide(orch, session, world: World, approval_id, tool="approve_refund"):
    ctx = ToolContext(
        actor=Actor(id=world.admin2.id, role=world.admin2.role), request_id="req-admin-1"
    )
    return await orch.executor.run(session, ctx, tool, {"approval_id": str(approval_id)})


async def run_row(session, run_id) -> WorkflowRun:
    (run,) = await all_rows(session, WorkflowRun, WorkflowRun.id == run_id)
    return run


async def last_text(session, run_id) -> str:
    msgs = sorted(
        await all_rows(session, Message, Message.workflow_run_id == run_id), key=lambda m: m.seq
    )
    return msgs[-1].content[0]["text"]


# --- waiting_for: what a waiting turn still waits for ---------------------------------------------


def row(**kw):
    return SimpleNamespace(**kw)


@pytest.mark.parametrize(
    ("approval", "refund", "expected"),
    [
        (ApprovalStatus.pending, RefundStatus.pending_approval, "approval"),
        (ApprovalStatus.approved, RefundStatus.processing, "payout"),
        (ApprovalStatus.approved, RefundStatus.succeeded, None),
        (ApprovalStatus.approved, RefundStatus.failed, None),
        (ApprovalStatus.rejected, RefundStatus.rejected, None),
        (ApprovalStatus.expired, RefundStatus.pending_approval, None),
    ],
)
def test_waiting_for_is_computed_from_the_approval_and_the_refund(approval, refund, expected):
    assert resume.waiting_for(row(status=approval), row(status=refund)) == expected  # type: ignore[arg-type]


def test_an_unusable_checkpoint_counts_as_still_waiting_never_as_final() -> None:
    assert resume.waiting_for(None, None) == "approval"
    assert resume.waiting_for(row(status=ApprovalStatus.approved), None) == "approval"  # type: ignore[arg-type]


# --- the rule: an approved refund that is still processing stays waiting -------------------------


async def test_a_decided_but_unfinished_payout_keeps_the_turn_waiting_until_it_resolves(
    db_session, world
) -> None:
    provider = SimulatedPaymentProvider()
    orch, run_id, approval_id = await waiting_turn(db_session, world, provider)
    before = await count(db_session, Message)

    provider.unavailable_next()  # the provider does not answer: the outcome is unknown
    out = await decide(orch, db_session, world, approval_id)
    assert out.http_status == 202  # approved, payout unconfirmed
    (refund,) = await all_rows(db_session, Refund)
    refund_id = refund.id
    assert refund.status == RefundStatus.processing

    run = await run_row(db_session, run_id)
    assert await resume.waiting_for_run(db_session, run) == "payout"
    assert await resume.finish_waiting(db_session, run_id) is False
    assert await resume.sync_waiting(db_session, run_id) is False  # the fallback obeys the rule too
    assert (await run_row(db_session, run_id)).status == WorkflowStatus.waiting_approval
    assert await count(db_session, Message) == before

    # the reconcile path re-runs the payout through the same finalise step, which emits the event
    outcome = await payout_flow(db_session, provider, refund_id, request_id="reconcile")
    assert outcome.kind == "succeeded"
    events = [e.event_type for e in await all_rows(db_session, OutboxEvent)]
    assert events.count("refund.completed") == 1

    assert await resume.finish_waiting(db_session, run_id) is True
    assert "approved and has been paid" in await last_text(db_session, run_id)
    assert await resume.finish_waiting(db_session, run_id) is False  # exactly once
    assert await count(db_session, Message) == before + 1


async def test_a_payout_that_the_reconciler_finds_declined_finishes_the_turn_as_failed(
    db_session, world
) -> None:
    provider = SimulatedPaymentProvider()
    orch, run_id, approval_id = await waiting_turn(db_session, world, provider)
    provider.unavailable_next()
    await decide(orch, db_session, world, approval_id)
    (refund,) = await all_rows(db_session, Refund)

    provider.fail_next()
    assert (await payout_flow(db_session, provider, refund.id, request_id="r")).kind == "failed"
    assert [e.event_type for e in await all_rows(db_session, OutboxEvent)].count(
        "refund.failed"
    ) == 1

    assert await resume.finish_waiting(db_session, run_id) is True
    assert "payout failed" in await last_text(db_session, run_id)
    assert (await run_row(db_session, run_id)).status == WorkflowStatus.completed


async def test_a_rejection_is_final_at_once(db_session, world) -> None:
    provider = SimulatedPaymentProvider()
    orch, run_id, approval_id = await waiting_turn(db_session, world, provider)
    run = await run_row(db_session, run_id)
    assert await resume.waiting_for_run(db_session, run) == "approval"
    assert await resume.finish_waiting(db_session, run_id) is False  # nobody decided yet

    await decide(orch, db_session, world, approval_id, tool="reject_refund")
    assert await resume.finish_waiting(db_session, run_id) is True
    assert "did not approve" in await last_text(db_session, run_id)


async def test_a_successful_approval_finishes_with_the_paid_message(db_session, world) -> None:
    provider = SimulatedPaymentProvider()
    orch, run_id, approval_id = await waiting_turn(db_session, world, provider)
    await decide(orch, db_session, world, approval_id)  # the payout succeeds in the same call
    assert await resume.finish_waiting(db_session, run_id) is True
    assert "approved and has been paid" in await last_text(db_session, run_id)


async def test_finishing_never_calls_the_model_or_runs_a_tool(db_session, world) -> None:
    llm = ObedientFakeLLM()
    provider = SimulatedPaymentProvider()
    orch = make_orchestrator(llm, provider)
    r = (await chat(orch, db_session, world.alice, ask_refund(world, "700"))).response
    run_id = uuid.UUID(r["workflow_id"])
    (approval,) = await all_rows(db_session, Approval)
    await decide(orch, db_session, world, approval.id)
    calls_before = len(llm.calls)
    tool_calls_before = await count(db_session, ToolCall)

    assert await resume.finish_waiting(db_session, run_id) is True

    assert len(llm.calls) == calls_before  # the model was not asked anything
    assert await count(db_session, ToolCall) == tool_calls_before  # and no tool ran


async def test_a_workflow_that_is_not_waiting_is_left_alone(db_session, world) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    r = (await chat(orch, db_session, world.alice, "hello")).response
    run_id = uuid.UUID(r["workflow_id"])
    before = await count(db_session, Message)
    assert (await run_row(db_session, run_id)).status == WorkflowStatus.completed
    assert await resume.finish_waiting(db_session, run_id) is False
    assert await resume.finish_waiting(db_session, uuid.uuid4()) is False  # unknown id
    assert await count(db_session, Message) == before


async def test_a_checkpoint_that_names_another_users_refund_is_never_trusted(
    db_session, world
) -> None:
    provider = SimulatedPaymentProvider()
    orch, run_id, approval_id = await waiting_turn(db_session, world, provider)
    await decide(orch, db_session, world, approval_id)
    run = await run_row(db_session, run_id)
    run.user_id = world.bob.id  # as if the checkpoint pointed at somebody else's refund
    await db_session.commit()
    assert await resume.finish_waiting(db_session, run_id) is False
    assert (await run_row(db_session, run_id)).status == WorkflowStatus.waiting_approval


# --- GET /workflows/{id} reports waiting_for -------------------------------


@pytest.fixture
async def client(db_session) -> AsyncIterator[tuple[AsyncClient, SimulatedPaymentProvider, list]]:
    provider = SimulatedPaymentProvider()
    orch = make_orchestrator(ObedientFakeLLM(), provider)

    async def _session():
        yield db_session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_orchestrator] = lambda: orch
    app.dependency_overrides[get_executor] = lambda: orch.executor
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, provider, [orch]
    for dep in (get_session, get_orchestrator, get_executor):
        app.dependency_overrides.pop(dep, None)


async def test_the_workflow_view_says_what_the_turn_waits_for(db_session, world, client) -> None:
    http, provider, (orch,) = client
    body = {"message": ask_refund(world, "700"), "message_id": str(uuid.uuid4())}
    r = await http.post("/api/v1/chat", json=body, headers=auth_for(world.alice.id))
    wf_id = r.json()["workflow_id"]
    (approval,) = await all_rows(db_session, Approval)
    approval_id = approval.id

    async def view():
        return (
            await http.get(f"/api/v1/workflows/{wf_id}", headers=auth_for(world.alice.id))
        ).json()

    v = await view()
    assert (v["status"], v["waiting_for"], v["waiting_for_approval"]) == (
        "waiting_approval",
        "approval",
        True,
    )

    provider.unavailable_next()
    await decide(orch, db_session, world, approval_id)
    v = await view()
    assert (v["status"], v["waiting_for"]) == ("waiting_approval", "payout")

    (refund,) = await all_rows(db_session, Refund)
    await payout_flow(db_session, provider, refund.id, request_id="reconcile")
    v = await view()  # the fallback finishes it on this read
    assert (v["status"], v["waiting_for"], v["waiting_for_approval"]) == ("completed", None, False)
    assert "approved and has been paid" in v["latest_message"]


async def test_a_workflow_that_never_waited_has_waiting_for_null(db_session, world, client) -> None:
    http, _, _ = client
    body = {"message": "hello", "message_id": str(uuid.uuid4())}
    r = await http.post("/api/v1/chat", json=body, headers=auth_for(world.alice.id))
    v = (
        await http.get(
            f"/api/v1/workflows/{r.json()['workflow_id']}", headers=auth_for(world.alice.id)
        )
    ).json()
    assert v["waiting_for"] is None and v["status"] == "completed"
    assert (await db_session.scalars(select(Refund))).all() == []
