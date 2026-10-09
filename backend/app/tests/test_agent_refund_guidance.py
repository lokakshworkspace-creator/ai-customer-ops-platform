"""The two refund behaviours the assistant is GUIDED towards (see the note in app/agent/prompts.py):

1. ask for the reason instead of choosing one;
2. after a refund on an unpaid order is refused, OFFER to cancel it, and cancel only after a yes.

Two kinds of test, and the difference matters. The first kind checks that the guidance text is
still in the prompt and in the tool descriptions, so deleting it fails a test. The second kind plays
a SCRIPTED model through the real orchestrator, executor and database to show what the BACKEND does
with those conversations: no refund exists until a request_refund is proposed, and no order is
cancelled until a cancel_order is proposed in a later message. **Neither kind can prove how a real
model behaves**; that is only known by trying it with the real model."""

import uuid

import pytest

from app.agent.llm.fake import ScriptedLLM, call, say
from app.agent.prompts import SYSTEM_PROMPT
from app.db.models import Order, Refund, ToolCall
from app.db.models.enums import OrderStatus, ToolCallStatus
from app.policy.rules import get_rules
from app.tests.agent_helpers import all_rows, chat, make_orchestrator
from app.tests.worlds import World, make_world
from app.tools.default import build_default_registry


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


# --- the guidance is in the text the model is given ---------------------------------------------


def test_the_prompt_tells_the_model_the_reason_is_the_customers() -> None:
    assert "THE REASON IS THE CUSTOMER'S, NEVER YOURS" in SYSTEM_PROMPT
    assert "What is the reason for the refund?" in SYSTEM_PROMPT
    assert 'never choose "other" on your own initiative' in SYSTEM_PROMPT


def test_the_prompt_tells_the_model_to_offer_cancelling_an_unpaid_order_and_wait_for_a_yes() -> (
    None
):
    assert "UNPAID ORDERS" in SYSTEM_PROMPT
    assert "OFFER to cancel" in SYSTEM_PROMPT
    assert "ONLY after the customer has said yes in a later message" in SYSTEM_PROMPT


def test_the_tool_descriptions_repeat_the_rules() -> None:
    reg = build_default_registry(get_rules())
    refund = reg.get("request_refund")
    cancel = reg.get("cancel_order")
    assert refund and cancel
    assert "never choose a reason yourself" in refund.description
    assert "offer to cancel them instead" in refund.description
    assert "explicitly agreed" in cancel.description
    reason = refund.input_schema()["properties"]["reason_code"]["description"]
    assert "never choose one yourself" in reason


def test_the_reason_field_is_still_required_and_the_schema_shape_is_unchanged() -> None:
    schema = build_default_registry(get_rules()).get("request_refund").input_schema()  # type: ignore[union-attr]
    assert set(schema["required"]) == {"order_id", "amount", "reason_code"}
    assert set(schema["properties"]) == {"order_id", "amount", "reason_code", "note"}
    assert schema["properties"]["reason_code"]["type"] == "string"


# --- what the backend does with those conversations -------------------------------------------


async def test_asking_for_the_reason_creates_no_refund_and_no_refund_call(
    db_session, world
) -> None:
    llm = ScriptedLLM(say("What is the reason for the refund?"))
    r = (
        await chat(
            make_orchestrator(llm), db_session, world.alice, f"refund my order {world.order}"
        )
    ).response

    assert r["reply"] == "What is the reason for the refund?" and r["tool_results"] == []
    assert llm.calls[0].system == SYSTEM_PROMPT
    assert await all_rows(db_session, Refund) == []
    assert await all_rows(db_session, ToolCall, ToolCall.tool_name == "request_refund") == []


async def test_the_refund_is_requested_only_after_the_customer_gives_the_reason(
    db_session, world
) -> None:
    llm = ScriptedLLM(
        say("What is the reason for the refund?"),
        call(
            "request_refund",
            order_id=str(world.order),
            amount="250.00",
            reason_code="damaged",
        ),
        say("Done."),
    )
    orch = make_orchestrator(llm)
    first = (
        await chat(orch, db_session, world.alice, f"refund 250 of order {world.order}")
    ).response
    assert await all_rows(db_session, Refund) == []

    cid = first["conversation_id"]
    second = (
        await chat(
            orch, db_session, world.alice, "it arrived damaged", conversation_id=uuid.UUID(cid)
        )
    ).response
    (refund,) = await all_rows(db_session, Refund)
    assert (
        refund.reason_code == "damaged" and second["tool_results"][-1]["tool"] == "request_refund"
    )


async def test_cancel_order_is_not_called_until_a_second_message_confirms(
    db_session, world
) -> None:
    llm = ScriptedLLM(
        # message 1: the refund on the unpaid order is refused by the backend, the model offers
        call(
            "request_refund",
            order_id=str(world.unpaid_order),
            amount="100.00",
            reason_code="changed_mind",
        ),
        say("Unpaid orders cannot be refunded. Would you like me to cancel it instead?"),
        # message 2: the customer said yes
        call("cancel_order", order_id=str(world.unpaid_order)),
        say("Your order is cancelled."),
    )
    orch = make_orchestrator(llm)
    first = (
        await chat(
            orch,
            db_session,
            world.alice,
            f"I changed my mind, refund my order {world.unpaid_order}",
        )
    ).response

    refused = first["tool_results"][0]
    assert refused["tool"] == "request_refund" and refused["status"] == "denied"
    assert "cancel" in first["reply"].lower()
    # the offer changed nothing
    assert await all_rows(db_session, ToolCall, ToolCall.tool_name == "cancel_order") == []
    (order,) = await all_rows(db_session, Order, Order.id == world.unpaid_order)
    assert order.status == OrderStatus.pending

    second = (
        await chat(
            orch,
            db_session,
            world.alice,
            "yes, please cancel it",
            conversation_id=uuid.UUID(first["conversation_id"]),
        )
    ).response
    (cancel,) = await all_rows(db_session, ToolCall, ToolCall.tool_name == "cancel_order")
    assert cancel.status == ToolCallStatus.succeeded
    (order,) = await all_rows(db_session, Order, Order.id == world.unpaid_order)
    assert order.status == OrderStatus.cancelled
    assert second["tool_results"][-1]["tool"] == "cancel_order"
