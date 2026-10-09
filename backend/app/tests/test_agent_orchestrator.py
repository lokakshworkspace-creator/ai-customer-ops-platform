"""The orchestrator end to end against the real executor, policy engine and database, with a fake
model. (The prompt-injection tests are in test_agent_injection.py.)"""

import uuid

import pytest
from sqlalchemy import text

from app.agent import resume
from app.agent.llm.base import LLMConfigError, LLMUnavailable, TextBlock
from app.agent.llm.fake import ObedientFakeLLM, ScriptedLLM, call, calls, say
from app.agent.orchestrator import (
    AssistantUnavailable,
    ConversationNotFound,
    MessageIdReused,
    TurnBudgetExceeded,
    TurnInProgress,
)
from app.db.models import Approval, Message, OutboxEvent, Refund, Ticket, ToolCall, WorkflowRun
from app.db.models.enums import (
    ApprovalStatus,
    MessageRole,
    RefundStatus,
    ToolCallStatus,
    WorkflowStatus,
)
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.policy.types import Actor
from app.tests.agent_helpers import all_rows, chat, count, make_orchestrator
from app.tests.worlds import World, make_world
from app.tools.context import ToolContext


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


def ask_refund(world: World, amount: str) -> str:
    return f"my order {world.order} arrived damaged, refund {amount}"


async def run_row(session, workflow_id: str) -> WorkflowRun:
    (run,) = await all_rows(session, WorkflowRun, WorkflowRun.id == uuid.UUID(workflow_id))
    return run


# --- the happy paths ----------------------------------------------------------------------------


async def test_a_small_refund_goes_through_the_executor_and_is_paid(db_session, world) -> None:
    provider = SimulatedPaymentProvider()
    llm = ObedientFakeLLM()
    orch = make_orchestrator(llm, provider)
    result = await chat(orch, db_session, world.alice, ask_refund(world, "250"))
    r = result.response

    assert r["status"] == "completed" and r["llm"] == "fake" and result.replayed is False
    assert [t["tool"] for t in r["tool_results"]] == ["list_orders", "request_refund"]
    assert r["tool_results"][1]["summary"] == "Refund of 250.00 INR: succeeded"
    (refund,) = await all_rows(db_session, Refund)
    assert (refund.status, str(refund.amount), refund.requested_by) == (
        RefundStatus.succeeded,
        "250.00",
        world.alice.id,
    )
    assert refund.idempotency_key.startswith("agent:")  # derived by the backend
    assert len(provider.ledger) == 1
    # every call went through the executor and is logged against this workflow
    calls_ = await all_rows(
        db_session, ToolCall, ToolCall.workflow_run_id == uuid.UUID(r["workflow_id"])
    )
    assert sorted(c.tool_name for c in calls_) == ["list_orders", "request_refund"]
    assert all(c.status == ToolCallStatus.succeeded and c.user_id == world.alice.id for c in calls_)
    run = await run_row(db_session, r["workflow_id"])
    assert (run.state, run.status) == ("DONE", WorkflowStatus.completed)
    assert run.response == r and run.context["tool_calls"] == 2


async def test_the_conversation_is_stored_in_order(db_session, world) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    r = (await chat(orch, db_session, world.alice, ask_refund(world, "250"))).response
    rows = await all_rows(
        db_session, Message, Message.conversation_id == uuid.UUID(r["conversation_id"])
    )
    rows.sort(key=lambda m: m.seq)
    assert [m.role for m in rows] == [
        MessageRole.user,
        MessageRole.assistant,
        MessageRole.tool,
        MessageRole.assistant,
        MessageRole.tool,
        MessageRole.assistant,
    ]
    assert {m.user_id for m in rows} == {world.alice.id}
    assert rows[0].content == [{"type": "text", "text": ask_refund(world, "250")}]
    assert rows[-1].content[0]["text"] == r["reply"]


async def test_a_large_refund_waits_for_a_human(db_session, world) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    r = (await chat(orch, db_session, world.alice, ask_refund(world, "700"))).response

    assert r["status"] == "waiting_approval"
    assert "approv" in r["reply"].lower()
    assert r["tool_results"][-1]["status"] == "awaiting_approval"
    assert r["tool_results"][-1]["reason_code"] == "REFUND_ABOVE_AUTO_LIMIT"
    (refund,) = await all_rows(db_session, Refund)
    (approval,) = await all_rows(db_session, Approval)
    assert (
        refund.status == RefundStatus.pending_approval and approval.status == ApprovalStatus.pending
    )
    run = await run_row(db_session, r["workflow_id"])
    assert (run.state, run.status) == ("WAITING_FOR_HUMAN", WorkflowStatus.waiting_approval)
    assert run.context["approval_id"] == str(approval.id) and run.context["refund_id"] == str(
        refund.id
    )


async def test_a_refund_the_policy_forbids_is_denied_logged_and_creates_nothing(
    db_session, world
) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    r = (await chat(orch, db_session, world.alice, ask_refund(world, "50000"))).response

    assert r["status"] == "completed"  # the turn finished; the denial is reported, not hidden
    assert r["tool_results"][-1]["status"] == "denied"
    assert r["tool_results"][-1]["reason_code"] == "AMOUNT_EXCEEDS_ORDER"
    assert "denied" in r["reply"] or "AMOUNT_EXCEEDS_ORDER" in r["reply"]
    assert await count(db_session, Refund) == 0 and await count(db_session, Approval) == 0
    assert await count(db_session, OutboxEvent) == 0
    (denied,) = await all_rows(db_session, ToolCall, ToolCall.status == ToolCallStatus.denied)
    assert denied.policy_reason_code == "AMOUNT_EXCEEDS_ORDER"


# --- message_id: replay and independence --------------------------------------------------------


async def test_the_same_message_id_twice_is_a_replay_with_one_refund_and_no_new_model_call(
    db_session, world
) -> None:
    provider = SimulatedPaymentProvider()
    llm = ObedientFakeLLM()
    orch = make_orchestrator(llm, provider)
    mid = uuid.uuid4()
    first = await chat(orch, db_session, world.alice, ask_refund(world, "250"), message_id=mid)
    seen_calls, seen_tool_calls = len(llm.calls), await count(db_session, ToolCall)

    second = await chat(orch, db_session, world.alice, ask_refund(world, "250"), message_id=mid)

    assert second.response == first.response and second.replayed is True  # identical
    assert len(llm.calls) == seen_calls  # the model was not called again
    assert await count(db_session, ToolCall) == seen_tool_calls  # no tool ran again
    assert await count(db_session, Refund) == 1 and len(provider.ledger) == 1
    # also with the conversation id the client learned from the first reply
    third = await chat(
        orch,
        db_session,
        world.alice,
        "ignored",
        message_id=mid,
        conversation_id=uuid.UUID(first.response["conversation_id"]),
    )
    assert third.response == first.response


async def test_a_message_id_cannot_be_moved_to_another_conversation(db_session, world) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    mid = uuid.uuid4()
    await chat(orch, db_session, world.alice, "hello", message_id=mid)
    other = (await chat(orch, db_session, world.alice, "hi again")).response["conversation_id"]
    with pytest.raises(MessageIdReused):
        await chat(
            orch, db_session, world.alice, "hello", message_id=mid, conversation_id=uuid.UUID(other)
        )


async def test_two_different_message_ids_for_the_same_request_are_each_judged_by_policy(
    db_session, world
) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    cid = None
    outcomes = []
    for _ in range(3):  # order total 1000.00: 400 + 400 fit, the third 400 exceeds what remains
        r = (
            await chat(orch, db_session, world.alice, ask_refund(world, "400"), conversation_id=cid)
        ).response
        cid = uuid.UUID(r["conversation_id"])
        outcomes.append((r["tool_results"][-1]["status"], r["tool_results"][-1]["reason_code"]))
    assert outcomes == [
        ("succeeded", "REFUND_ALLOWED"),
        ("succeeded", "REFUND_ALLOWED"),
        ("denied", "AMOUNT_EXCEEDS_REMAINING"),
    ]
    keys = [r.idempotency_key for r in await all_rows(db_session, Refund)]
    assert len(keys) == 2 and len(set(keys)) == 2


async def test_the_same_proposal_twice_in_one_turn_pays_once(db_session, world) -> None:
    provider = SimulatedPaymentProvider()
    args = {"order_id": str(world.order), "amount": "100.00", "reason_code": "damaged"}
    llm = ScriptedLLM(
        calls(
            ("request_refund", args), ("request_refund", dict(args, amount="100"))
        ),  # 100 == 100.00
        say("done"),
    )
    r = (
        await chat(make_orchestrator(llm, provider), db_session, world.alice, "refund 100")
    ).response
    assert [t["status"] for t in r["tool_results"]] == ["succeeded", "succeeded"]
    assert (
        await count(db_session, Refund) == 1 and len(provider.ledger) == 1
    )  # the 2nd was a replay


# --- limits -------------------------------------------------------------------------------------


async def test_a_looping_model_is_stopped_at_five_calls_and_escalated_with_a_ticket(
    db_session, world
) -> None:
    llm = ScriptedLLM(*[call("list_orders")] * 10)
    orch = make_orchestrator(llm)
    r = (await chat(orch, db_session, world.alice, "orders orders orders")).response

    assert r["status"] == "escalated" and "support team" in r["reply"]
    run_id = uuid.UUID(r["workflow_id"])
    tool_calls = await all_rows(db_session, ToolCall, ToolCall.workflow_run_id == run_id)
    assert sorted(c.tool_name for c in tool_calls) == ["create_ticket"] + ["list_orders"] * 5
    (ticket,) = await all_rows(db_session, Ticket)
    assert ticket.user_id == world.alice.id and "tool-call limit" in ticket.body
    assert len(llm.calls) == 6  # the sixth proposal was refused, not executed
    run = await run_row(db_session, r["workflow_id"])
    assert (run.state, run.status) == ("ESCALATED", WorkflowStatus.escalated)


async def test_the_per_conversation_limit_escalates_later_turns_without_calling_the_model(
    db_session, world
) -> None:
    llm = ScriptedLLM(call("list_orders"), call("list_orders"), say("two lists"))
    orch = make_orchestrator(llm, agent_max_tool_calls_per_conversation=2)
    first = (await chat(orch, db_session, world.alice, "orders")).response
    assert first["status"] == "completed"
    cid = uuid.UUID(first["conversation_id"])

    second = (
        await chat(orch, db_session, world.alice, "more orders", conversation_id=cid)
    ).response
    assert second["status"] == "escalated" and len(llm.calls) == 3  # no new model call
    third = (await chat(orch, db_session, world.alice, "even more", conversation_id=cid)).response
    assert third["status"] == "escalated"
    assert await count(db_session, Ticket) == 1  # one ticket per conversation, not one per message


# --- model failures -----------------------------------------------------------------------------


async def test_when_the_model_is_down_before_anything_ran_the_turn_is_undone_and_can_be_retried(
    db_session, world
) -> None:
    mid = uuid.uuid4()
    down = make_orchestrator(
        ScriptedLLM(LLMUnavailable("The assistant is temporarily unavailable."))
    )
    with pytest.raises(AssistantUnavailable, match="temporarily unavailable"):
        await chat(down, db_session, world.alice, "hello", message_id=mid)
    assert await count(db_session, WorkflowRun) == 0 and await count(db_session, Message) == 0

    up = make_orchestrator(ScriptedLLM(say("Hi there")))
    ok = await chat(up, db_session, world.alice, "hello", message_id=mid)  # the SAME message id
    assert ok.response["reply"] == "Hi there" and ok.replayed is False


async def test_a_misconfigured_model_is_also_a_clear_503(db_session, world) -> None:
    orch = make_orchestrator(
        ScriptedLLM(LLMConfigError("The assistant is not configured correctly."))
    )
    with pytest.raises(AssistantUnavailable, match="not configured"):
        await chat(orch, db_session, world.alice, "hello")


async def test_when_the_model_dies_after_a_tool_ran_the_turn_is_kept_and_says_what_was_done(
    db_session, world
) -> None:
    args = {"order_id": str(world.order), "amount": "100.00", "reason_code": "damaged"}
    provider = SimulatedPaymentProvider()
    llm = ScriptedLLM(call("request_refund", **args), LLMUnavailable("gone"))
    orch = make_orchestrator(llm, provider)
    mid = uuid.uuid4()
    result = await chat(orch, db_session, world.alice, "refund 100", message_id=mid)
    r = result.response

    assert r["status"] == "failed" and "Refund of 100.00 INR: succeeded" in r["reply"]
    assert await count(db_session, Refund) == 1  # the refund is real, so the turn is kept
    again = await chat(orch, db_session, world.alice, "refund 100", message_id=mid)
    assert again.response == r and again.replayed is True


async def test_an_unexpected_error_before_any_tool_undoes_the_turn(db_session, world) -> None:
    orch = make_orchestrator(ScriptedLLM(RuntimeError("bug")))
    with pytest.raises(RuntimeError, match="bug"):
        await chat(orch, db_session, world.alice, "hello")
    assert await count(db_session, WorkflowRun) == 0


async def test_a_turn_that_exceeds_its_deadline_is_a_clear_503(db_session, world) -> None:
    import asyncio

    async def slow(_seen):
        await asyncio.sleep(5)

    class Slow(ScriptedLLM):
        async def complete(self, **kw):
            await slow(None)

    orch = make_orchestrator(Slow(), agent_turn_deadline_s=0.2)
    with pytest.raises(AssistantUnavailable, match="too long"):
        await chat(orch, db_session, world.alice, "hello")
    assert await count(db_session, WorkflowRun) == 0


# --- what the model is sent, and when -----------------------------------------------------------


async def test_no_database_transaction_is_open_while_the_model_thinks(db_session, world) -> None:
    llm = ObedientFakeLLM()
    open_during_calls: list[bool] = []
    llm.on_call = lambda _c: open_during_calls.append(db_session.in_transaction())
    orch = make_orchestrator(llm)
    await chat(orch, db_session, world.alice, ask_refund(world, "250"))
    assert len(open_during_calls) == 3 and not any(open_during_calls)


async def test_every_call_sends_max_tokens_and_a_bounded_history(db_session, world) -> None:
    llm = ScriptedLLM(*[say(f"reply {i}") for i in range(6)])
    orch = make_orchestrator(llm, llm_max_tokens=77, llm_history_messages=4)
    cid = None
    for i in range(6):
        r = (
            await chat(orch, db_session, world.alice, f"message {i}", conversation_id=cid)
        ).response
        cid = uuid.UUID(r["conversation_id"])
    assert len(llm.calls) == 6
    assert {c.max_tokens for c in llm.calls} == {77}
    assert all(len(c.messages) <= 4 and c.messages[0].role == "user" for c in llm.calls)
    assert llm.calls[-1].messages[-1].content == (TextBlock("message 5"),)  # the newest is kept


async def test_the_model_is_shown_only_agent_visible_tools_and_the_system_prompt(
    db_session, world
) -> None:
    llm = ScriptedLLM(say("hi"))
    await chat(make_orchestrator(llm), db_session, world.admin, "hello")  # even for an ADMIN
    (seen,) = llm.calls
    assert {t.name for t in seen.tools} == {
        "get_order",
        "list_orders",
        "create_ticket",
        "cancel_order",
        "request_refund",
        "search_knowledge_base",
    }
    assert "never" in seen.system and "approve_refund" in seen.system  # told it has no admin tools


async def test_a_cut_off_proposal_is_not_executed(db_session, world) -> None:
    from app.agent.llm.base import LLMResponse, ToolUseBlock

    truncated = LLMResponse(
        (ToolUseBlock("t1", "request_refund", {"order_id": "x"}),), "max_tokens"
    )
    r = (
        await chat(make_orchestrator(ScriptedLLM(truncated)), db_session, world.alice, "refund")
    ).response
    assert r["tool_results"] == [] and await count(db_session, ToolCall) == 0


# --- conversations belong to their user; turns do not collide -----------------------------------


async def test_a_conversation_is_private_to_its_user(db_session, world) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    cid = uuid.UUID(
        (await chat(orch, db_session, world.alice, "hello")).response["conversation_id"]
    )
    for stranger in (world.bob, world.admin):
        with pytest.raises(ConversationNotFound):
            await chat(orch, db_session, stranger, "hi", conversation_id=cid)
    with pytest.raises(ConversationNotFound):
        await chat(orch, db_session, world.alice, "hi", conversation_id=uuid.uuid4())  # unknown id


async def test_a_message_id_is_per_user(db_session, world) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    mid = uuid.uuid4()
    a = await chat(orch, db_session, world.alice, "hello", message_id=mid)
    b = await chat(orch, db_session, world.bob, "hello", message_id=mid)
    assert b.replayed is False and a.response["conversation_id"] != b.response["conversation_id"]


async def test_a_turn_in_flight_blocks_the_next_one_and_a_dead_turn_does_not(
    db_session, world
) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    cid = uuid.UUID(
        (await chat(orch, db_session, world.alice, "hello")).response["conversation_id"]
    )
    await db_session.execute(
        text(
            "INSERT INTO workflow_runs (id, user_id, status, state, conversation_id, message_id) "
            "VALUES (gen_random_uuid(), :u, 'running', 'PROPOSE', :c, :m)"
        ),
        {"u": world.alice.id, "c": cid, "m": uuid.uuid4()},
    )
    await db_session.commit()

    with pytest.raises(TurnInProgress):
        await chat(orch, db_session, world.alice, "again", conversation_id=cid)

    # the same message_id as the in-flight one: also "in progress", not a second run
    (stuck,) = await all_rows(db_session, WorkflowRun, WorkflowRun.status == WorkflowStatus.running)
    stuck_id, mid = stuck.id, stuck.message_id
    with pytest.raises(TurnInProgress):
        await chat(orch, db_session, world.alice, "again", message_id=mid)

    # a process that died mid-turn: its run is old, so it is abandoned and the conversation is free
    await db_session.execute(
        text(
            "UPDATE workflow_runs SET updated_at = now() - interval '1 hour' "
            "WHERE status = 'running'"
        )
    )
    await db_session.commit()
    ok = await chat(orch, db_session, world.alice, "again", conversation_id=cid)
    assert ok.response["status"] == "completed"
    assert (await run_row(db_session, str(stuck_id))).status == WorkflowStatus.failed


async def test_the_hourly_turn_budget_stops_runaway_use_but_never_blocks_a_replay(
    db_session, world
) -> None:
    orch = make_orchestrator(ObedientFakeLLM(), agent_turns_per_hour=2)
    mid = uuid.uuid4()
    await chat(orch, db_session, world.alice, "one", message_id=mid)
    await chat(orch, db_session, world.alice, "two")
    with pytest.raises(TurnBudgetExceeded):
        await chat(orch, db_session, world.alice, "three")
    assert (await chat(orch, db_session, world.alice, "one", message_id=mid)).replayed is True
    await chat(orch, db_session, world.bob, "bob is unaffected")


# --- waiting for a human, and the stubbed resume ------------------------------------------------


async def approve(orch, session, world: World, approval_id: uuid.UUID, tool="approve_refund"):
    ctx = ToolContext(
        actor=Actor(id=world.admin2.id, role=world.admin2.role), request_id="req-admin-1"
    )
    return await orch.executor.run(session, ctx, tool, {"approval_id": str(approval_id)})


async def test_sync_finishes_a_waiting_workflow_once_and_only_once(db_session, world) -> None:
    orch = make_orchestrator(ObedientFakeLLM())
    r = (await chat(orch, db_session, world.alice, ask_refund(world, "700"))).response
    run_id = uuid.UUID(r["workflow_id"])
    (approval,) = await all_rows(db_session, Approval)
    approval_id = approval.id  # ORM objects expire on rollback: keep the id
    before = await count(db_session, Message)

    assert await resume.sync_waiting(db_session, run_id) is False  # nobody decided yet
    assert await count(db_session, Message) == before

    out = await approve(orch, db_session, world, approval_id)
    assert out.status == ToolCallStatus.succeeded
    assert await resume.sync_waiting(db_session, run_id) is True
    assert await resume.sync_waiting(db_session, run_id) is False  # the second call is a no-op
    assert await resume.sync_waiting(db_session, run_id) is False
    assert await count(db_session, Message) == before + 1  # exactly one message was added

    run = await run_row(db_session, r["workflow_id"])
    assert (run.state, run.status) == ("DONE", WorkflowStatus.completed)
    assert run.context["outcome"] == {"approval_status": "approved", "refund_status": "succeeded"}
    assert run.response == r  # the original response is frozen
    msgs = sorted(
        await all_rows(db_session, Message, Message.workflow_run_id == run_id), key=lambda m: m.seq
    )
    assert "approved and has been paid" in msgs[-1].content[0]["text"]


async def test_a_rejection_is_reported_and_the_next_turn_already_knows(db_session, world) -> None:
    llm = ObedientFakeLLM()
    orch = make_orchestrator(llm)
    r = (await chat(orch, db_session, world.alice, ask_refund(world, "700"))).response
    (approval,) = await all_rows(db_session, Approval)
    approval_id = approval.id
    await approve(orch, db_session, world, approval_id, tool="reject_refund")

    # no explicit sync: the next message in the conversation triggers it before the model is called
    cid = uuid.UUID(r["conversation_id"])
    seen = []
    llm.on_call = lambda c: seen.append(c.messages)
    await chat(orch, db_session, world.alice, "what happened?", conversation_id=cid)
    flat = " ".join(b.text for m in seen[0] for b in m.content if isinstance(b, TextBlock))
    assert "did not approve your refund of 700.00 INR" in flat
    assert (await run_row(db_session, r["workflow_id"])).status == WorkflowStatus.completed
