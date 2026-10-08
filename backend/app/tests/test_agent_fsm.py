"""The pure parts of the agent: the state machine, derived idempotency keys, history trimming and
what the model / UI are shown of a tool result. No database."""

import re
import uuid
from decimal import Decimal

import pytest

from app.agent.fsm import (
    STATUS_OF,
    TERMINAL,
    TRANSITIONS,
    Caps,
    IllegalTransition,
    State,
    check_transition,
)
from app.agent.history import trim_history
from app.agent.keys import canonical_args, derive_idempotency_key
from app.agent.llm.base import Message, TextBlock, ToolResultBlock, ToolUseBlock
from app.agent.results_view import MAX_RESULT_BYTES, model_view, tool_result_text, ui_summary
from app.db.models.enums import PolicyDecision, ToolCallStatus, WorkflowStatus
from app.modules.refunds.hashing import is_valid_idempotency_key
from app.tools.executor import ToolOutcome
from app.tools.schemas import RequestRefundInput

CONV, MSG = uuid.uuid4(), uuid.uuid4()
ORDER = uuid.uuid4()


# --- state machine ---------------------------------------------------------------------------


def test_every_state_has_a_transition_entry_and_a_status() -> None:
    assert set(TRANSITIONS) == set(State) == set(STATUS_OF)
    assert set(STATUS_OF.values()) == set(WorkflowStatus)  # every DB status is reachable


def test_terminal_states_have_no_exit_and_the_rest_do() -> None:
    assert TERMINAL == {State.DONE, State.FAILED, State.ESCALATED}
    for state in State:
        assert bool(TRANSITIONS[state]) == (state not in TERMINAL)


@pytest.mark.parametrize(("a", "b"), [(a, b) for a, nxt in TRANSITIONS.items() for b in nxt])
def test_legal_transitions_pass(a: State, b: State) -> None:
    check_transition(a, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [(a, b) for a in State for b in State if b not in TRANSITIONS[a]],
)
def test_every_other_transition_is_refused(a: State, b: State) -> None:
    with pytest.raises(IllegalTransition):
        check_transition(a, b)


def test_the_loop_and_the_stubbed_resume_are_the_only_ways_around() -> None:
    assert State.PROPOSE in TRANSITIONS[State.EXECUTE]  # results go back to the model
    assert TRANSITIONS[State.WAITING_FOR_HUMAN] == {State.DONE}  # only resume.py leaves it
    assert STATUS_OF[State.WAITING_FOR_HUMAN] == WorkflowStatus.waiting_approval


def test_caps_stop_the_call_that_would_go_over() -> None:
    caps = Caps(per_turn=5, per_conversation=20)
    assert not caps.exceeded(0, 0) and not caps.exceeded(4, 19)
    assert caps.exceeded(5, 0)  # a 6th call in one turn
    assert caps.exceeded(0, 20)  # a 21st call in one conversation


# --- derived idempotency keys ----------------------------------------------------------------


def refund(amount="250.00", **kw) -> RequestRefundInput:
    return RequestRefundInput(order_id=ORDER, amount=Decimal(amount), reason_code="damaged", **kw)


def test_the_key_is_stable_and_valid() -> None:
    k1 = derive_idempotency_key(CONV, MSG, "request_refund", refund())
    k2 = derive_idempotency_key(CONV, MSG, "request_refund", refund())
    assert k1 == k2 and is_valid_idempotency_key(k1) and len(k1) <= 255
    assert k1.startswith(f"agent:{CONV}:{MSG}:request_refund:")


def test_equivalent_amounts_make_the_same_key_and_different_ones_do_not() -> None:
    base = derive_idempotency_key(CONV, MSG, "request_refund", refund("250.00"))
    assert derive_idempotency_key(CONV, MSG, "request_refund", refund("250")) == base
    assert derive_idempotency_key(CONV, MSG, "request_refund", refund("250.01")) != base
    assert derive_idempotency_key(CONV, MSG, "request_refund", refund(note="x")) != base


def test_a_different_message_conversation_or_tool_gives_a_different_key() -> None:
    base = derive_idempotency_key(CONV, MSG, "request_refund", refund())
    assert derive_idempotency_key(CONV, uuid.uuid4(), "request_refund", refund()) != base
    assert derive_idempotency_key(uuid.uuid4(), MSG, "request_refund", refund()) != base
    assert derive_idempotency_key(CONV, MSG, "cancel_order", refund()) != base


def test_raw_arguments_are_hashed_canonically_and_a_hostile_tool_name_stays_in_the_charset() -> (
    None
):
    assert canonical_args({"b": 1, "a": 2}) == canonical_args({"a": 2, "b": 1})
    key = derive_idempotency_key(CONV, MSG, "ev il/../tool\n", {"x": 1})
    assert re.fullmatch(r"[A-Za-z0-9._:\-]{8,255}", key)


def test_nothing_the_model_writes_can_become_the_key() -> None:
    a = derive_idempotency_key(CONV, MSG, "request_refund", {"idempotency_key": "mine-123456"})
    b = derive_idempotency_key(CONV, MSG, "request_refund", {"idempotency_key": "other-123456"})
    assert "mine" not in a and "other" not in b  # the model's text only ever feeds a hash


# --- history -----------------------------------------------------------------------------------


def convo(turns: int) -> list[Message]:
    out: list[Message] = []
    for i in range(turns):
        out += [
            Message.user(f"u{i}"),
            Message.assistant(ToolUseBlock(f"t{i}", "list_orders", {})),
            Message.tool(ToolResultBlock(f"t{i}", "{}")),
            Message.assistant(TextBlock(f"a{i}")),
        ]
    return out


def test_history_is_cut_to_the_limit_and_starts_at_a_user_message() -> None:
    msgs = convo(10)  # 40 messages
    out = trim_history(msgs, 20)
    assert len(out) <= 20 and out[0].role == "user"
    assert out[-1] == msgs[-1]


def test_a_cut_never_leaves_an_orphan_tool_result() -> None:
    for limit in range(1, 41):
        out = trim_history(convo(10), limit)
        assert out and out[0].role == "user"
        ids_proposed = {b.id for m in out for b in m.content if isinstance(b, ToolUseBlock)}
        ids_answered = {
            b.tool_use_id for m in out for b in m.content if isinstance(b, ToolResultBlock)
        }
        assert ids_answered <= ids_proposed


def test_the_latest_user_message_is_always_kept_even_in_a_very_long_turn() -> None:
    msgs = [Message.user("old")] + convo(1)[1:] + [Message.user("now")]
    msgs += [
        m
        for i in range(12)
        for m in (
            Message.assistant(ToolUseBlock(f"x{i}", "list_orders", {})),
            Message.tool(ToolResultBlock(f"x{i}", "{}")),
        )
    ]
    out = trim_history(msgs, 5)
    assert Message.user("now") in out and out[0] == Message.user("now")


def test_history_edge_cases() -> None:
    assert trim_history([], 20) == []
    assert trim_history([Message.user("hi")], 1) == [Message.user("hi")]


# --- what the model and the UI see of a result ---------------------------------------------------


def outcome(tool: str, data: dict | None, status=ToolCallStatus.succeeded, **kw) -> ToolOutcome:
    return ToolOutcome(
        tool_call_id=uuid.uuid4(),
        tool_name=tool,
        status=status,
        http_status=200,
        decision=kw.pop("decision", PolicyDecision.ALLOW),
        reason_code=kw.pop("reason_code", "READ_ALLOWED"),
        reason=kw.pop("reason", "ok"),
        data=data,
        **kw,
    )


ORDER_JSON = {
    "id": str(ORDER),
    "user_id": str(uuid.uuid4()),
    "status": "delivered",
    "total_amount": "1000.00",
    "currency": "INR",
    "placed_at": "2026-10-01T00:00:00Z",
    "items": [
        {
            "id": "i",
            "sku": "S",
            "name": "Widget " + "x" * 200,
            "quantity": 1,
            "unit_price": "1000.00",
        }
    ],
}


def test_the_model_sees_an_allow_list_not_the_raw_result() -> None:
    view = model_view(
        outcome("list_orders", {"orders": [ORDER_JSON], "total": 1, "limit": 20, "offset": 0})
    )
    (order,) = view["data"]["orders"]
    assert "user_id" not in order and set(order) == {
        "id",
        "status",
        "total_amount",
        "currency",
        "placed_at",
        "items",
    }
    assert len(order["items"][0]["name"]) == 80 and "sku" not in order["items"][0]
    refund = model_view(
        outcome(
            "request_refund",
            {
                "refund": {
                    "id": "r",
                    "status": "succeeded",
                    "amount": "5.00",
                    "currency": "INR",
                    "order_id": "o",
                    "reason_code": "damaged",
                    "note": "SECRET NOTE",
                    "requested_by": str(uuid.uuid4()),
                },
                "approval_id": None,
            },
        )
    )
    assert "SECRET NOTE" not in tool_result_text(
        outcome("request_refund", {"refund": {"note": "SECRET NOTE", "status": "succeeded"}})
    )
    assert set(refund["data"]["refund"]) == {
        "id",
        "order_id",
        "amount",
        "currency",
        "status",
        "reason_code",
    }


def test_an_unknown_tool_shows_the_model_nothing_but_its_status() -> None:
    assert model_view(outcome("mystery", {"secret": 1}))["data"] == {}


def test_a_huge_result_is_cut_to_the_size_cap() -> None:
    orders = [dict(ORDER_JSON, id=str(uuid.uuid4())) for _ in range(100)]
    text = tool_result_text(outcome("list_orders", {"orders": orders, "total": 100}))
    assert len(text.encode()) <= MAX_RESULT_BYTES
    assert '"truncated":true' in text.replace(" ", "")


def test_errors_and_denials_are_reported_with_reasons_not_internals() -> None:
    failed = model_view(
        outcome(
            "cancel_order",
            None,
            ToolCallStatus.failed,
            decision=None,
            reason_code=None,
            reason=None,
            error_code="INTERNAL_ERROR",
            error="DB blew up: password=hunter2",
        )
    )
    assert "hunter2" not in json_text(failed) and failed["error_code"] == "INTERNAL_ERROR"
    denied = model_view(
        outcome(
            "request_refund",
            None,
            ToolCallStatus.denied,
            decision=PolicyDecision.DENY,
            reason_code="AMOUNT_EXCEEDS_ORDER",
            reason="The refund exceeds the order total.",
        )
    )
    assert denied["decision"] == "DENY" and denied["reason_code"] == "AMOUNT_EXCEEDS_ORDER"


def json_text(x) -> str:
    import json

    return json.dumps(x)


def test_the_ui_summary_is_built_by_the_backend() -> None:
    s = ui_summary(
        outcome(
            "request_refund",
            {"refund": {"status": "pending_approval", "amount": "700.00", "currency": "INR"}},
            ToolCallStatus.awaiting_approval,
            decision=PolicyDecision.REQUIRE_APPROVAL,
            reason_code="REFUND_ABOVE_AUTO_LIMIT",
        )
    )
    assert s["summary"] == "Refund of 700.00 INR: pending_approval"
    assert (s["decision"], s["reason_code"], s["status"]) == (
        "REQUIRE_APPROVAL",
        "REFUND_ABOVE_AUTO_LIMIT",
        "awaiting_approval",
    )
