"""Policy engine: pure, table-driven, boundary-focused. No database anywhere in this file."""

import ast
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

import app.policy as policy_pkg
from app.policy import (
    APPROVE_REFUND,
    REJECT_REFUND,
    CancelFacts,
    PolicyDecision,
    ReasonCode,
    evaluate_action_gate,
    evaluate_cancel_order,
    evaluate_create_ticket,
    evaluate_read,
    evaluate_refund_decision,
    evaluate_refund_request,
)
from app.policy.types import OrderStatus, Role
from app.tests.policy_helpers import DAY, NOW, actor, make_facts, make_rules

ALLOW, ASK, DENY = (
    PolicyDecision.ALLOW,
    PolicyDecision.REQUIRE_APPROVAL,
    PolicyDecision.DENY,
)
D = Decimal


def run(rules=None, **fact_overrides):
    return evaluate_refund_request(
        rules or make_rules(), actor(), make_facts(**fact_overrides), NOW
    )


# (id, fact overrides, expected decision, expected primary reason code)
REFUND_CASES = [
    # --- amount boundary around the auto-approve limit (500.00) ---
    ("amount-0.01", {"amount": D("0.01")}, ALLOW, ReasonCode.REFUND_ALLOWED),
    ("amount-499.99", {"amount": D("499.99")}, ALLOW, ReasonCode.REFUND_ALLOWED),
    ("amount-exactly-500", {"amount": D("500.00")}, ALLOW, ReasonCode.REFUND_ALLOWED),
    ("amount-500-int-form", {"amount": D("500")}, ALLOW, ReasonCode.REFUND_ALLOWED),
    ("amount-500.01", {"amount": D("500.01")}, ASK, ReasonCode.REFUND_ABOVE_AUTO_LIMIT),
    ("amount-999", {"amount": D("999.00")}, ASK, ReasonCode.REFUND_ABOVE_AUTO_LIMIT),
    # --- order age boundary (30 days, strictly older is denied) ---
    ("age-0", {"order_placed_at": NOW}, ALLOW, ReasonCode.REFUND_ALLOWED),
    (
        "age-29d-23h59m",
        {"order_placed_at": NOW - 30 * DAY + (DAY / 1440)},
        ALLOW,
        ReasonCode.REFUND_ALLOWED,
    ),
    ("age-exactly-30d", {"order_placed_at": NOW - 30 * DAY}, ALLOW, ReasonCode.REFUND_ALLOWED),
    (
        "age-30d-1s",
        {"order_placed_at": NOW - 30 * DAY - (DAY / 86400)},
        DENY,
        ReasonCode.ORDER_TOO_OLD,
    ),
    ("age-45d", {"order_placed_at": NOW - 45 * DAY}, DENY, ReasonCode.ORDER_TOO_OLD),
    # --- order status ---
    ("status-paid", {"order_status": OrderStatus.paid}, ALLOW, ReasonCode.REFUND_ALLOWED),
    ("status-delivered", {"order_status": OrderStatus.delivered}, ALLOW, ReasonCode.REFUND_ALLOWED),
    (
        "status-shipped",
        {"order_status": OrderStatus.shipped},
        DENY,
        ReasonCode.ORDER_NOT_REFUNDABLE,
    ),
    (
        "status-pending",
        {"order_status": OrderStatus.pending},
        DENY,
        ReasonCode.ORDER_NOT_REFUNDABLE,
    ),
    (
        "status-cancelled",
        {"order_status": OrderStatus.cancelled},
        DENY,
        ReasonCode.ORDER_NOT_REFUNDABLE,
    ),
    ("no-succeeded-payment", {"has_succeeded_payment": False}, DENY, ReasonCode.PAYMENT_NOT_FOUND),
    # --- amount vs order / remaining refundable ---
    (
        "exactly-order-total",
        {"amount": D("1000.00"), "order_total": D("1000.00")},
        ASK,
        ReasonCode.REFUND_ABOVE_AUTO_LIMIT,
    ),
    (
        "one-paisa-over-total",
        {"amount": D("1000.01"), "order_total": D("1000.00")},
        DENY,
        ReasonCode.AMOUNT_EXCEEDS_ORDER,
    ),
    (
        "50000-on-a-4999-order",
        {"amount": D("50000.00"), "order_total": D("4999.00")},
        DENY,
        ReasonCode.AMOUNT_EXCEEDS_ORDER,
    ),
    (
        "remaining-exactly-equal",
        {"amount": D("400.00"), "already_refunded": D("600.00")},
        ALLOW,
        ReasonCode.REFUND_ALLOWED,
    ),
    (
        "remaining-minus-one-paisa",
        {"amount": D("400.01"), "already_refunded": D("600.00")},
        DENY,
        ReasonCode.AMOUNT_EXCEEDS_REMAINING,
    ),
    (
        "fully-refunded-already",
        {"amount": D("0.01"), "already_refunded": D("1000.00")},
        DENY,
        ReasonCode.AMOUNT_EXCEEDS_REMAINING,
    ),
    # --- velocity: more than 3 refunds in the window escalates (the 4th request) ---
    ("velocity-prior-0", {"recent_refund_count": 0}, ALLOW, ReasonCode.REFUND_ALLOWED),
    ("velocity-prior-2-this-is-3rd", {"recent_refund_count": 2}, ALLOW, ReasonCode.REFUND_ALLOWED),
    ("velocity-prior-3-this-is-4th", {"recent_refund_count": 3}, ASK, ReasonCode.REFUND_VELOCITY),
    ("velocity-prior-10", {"recent_refund_count": 10}, ASK, ReasonCode.REFUND_VELOCITY),
    # --- reasons ---
    *[
        (f"reason-auto-{r}", {"reason_code": r}, ALLOW, ReasonCode.REFUND_ALLOWED)
        for r in (
            "damaged",
            "defective",
            "wrong_item",
            "not_as_described",
            "not_delivered",
            "late_delivery",
        )
    ],
    *[
        (f"reason-review-{r}", {"reason_code": r}, ASK, ReasonCode.REASON_NEEDS_REVIEW)
        for r in ("changed_mind", "order_cancelled", "other")
    ],
    ("reason-unknown", {"reason_code": "because"}, DENY, ReasonCode.INVALID_REASON),
    ("reason-empty", {"reason_code": ""}, DENY, ReasonCode.INVALID_REASON),
    # --- degenerate amounts ---
    ("amount-zero", {"amount": D("0.00")}, DENY, ReasonCode.INVALID_AMOUNT),
    ("amount-negative", {"amount": D("-5.00")}, DENY, ReasonCode.INVALID_AMOUNT),
    # --- currency ---
    ("currency-not-inr", {"order_currency": "USD"}, ASK, ReasonCode.CURRENCY_UNSUPPORTED),
]


@pytest.mark.parametrize(
    ("overrides", "decision", "code"),
    [pytest.param(o, d, c, id=i) for i, o, d, c in REFUND_CASES],
)
def test_refund_request_table(overrides, decision, code) -> None:
    result = run(**overrides)
    assert (result.decision, result.reason_code) == (decision, code)
    assert result.reason  # always a human-readable explanation
    assert result.reason_code in result.all_reason_codes


def test_deny_beats_require_approval_and_lists_every_deny() -> None:
    # too old AND above the limit AND an unreviewed reason: DENY wins, escalations are not listed
    r = run(
        order_placed_at=NOW - 45 * DAY,
        amount=D("900.00"),
        reason_code="changed_mind",
        order_status=OrderStatus.shipped,
    )
    assert r.decision == DENY
    assert set(r.all_reason_codes) == {ReasonCode.ORDER_NOT_REFUNDABLE, ReasonCode.ORDER_TOO_OLD}
    assert r.reason_code == ReasonCode.ORDER_NOT_REFUNDABLE  # first in the fixed precedence order


def test_every_escalation_is_listed_when_approval_is_required() -> None:
    r = run(
        amount=D("900.00"), reason_code="changed_mind", recent_refund_count=3, order_currency="USD"
    )
    assert r.decision == ASK
    assert set(r.all_reason_codes) == {
        ReasonCode.CURRENCY_UNSUPPORTED,
        ReasonCode.REFUND_ABOVE_AUTO_LIMIT,
        ReasonCode.REFUND_VELOCITY,
        ReasonCode.REASON_NEEDS_REVIEW,
    }


def test_details_are_json_safe_strings() -> None:
    r = run(amount=D("50000.00"), order_total=D("4999.00"))
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in r.details.items())
    assert r.details["amount"] == "50000.00"


# --- nothing is hardcoded: changing rules.yaml values changes the outcome ---------------


@pytest.mark.parametrize(
    ("rule_overrides", "fact_overrides", "default_outcome", "new_outcome"),
    [
        ({"refund.auto_approve_max": "1000.00"}, {"amount": D("800.00")}, ASK, ALLOW),
        ({"refund.auto_approve_max": "50.00"}, {"amount": D("100.00")}, ALLOW, ASK),
        ({"refund.max_order_age_days": 10}, {"order_placed_at": NOW - 15 * DAY}, ALLOW, DENY),
        ({"refund.max_order_age_days": 60}, {"order_placed_at": NOW - 45 * DAY}, DENY, ALLOW),
        ({"refund.velocity_max_refunds": 1}, {"recent_refund_count": 1}, ALLOW, ASK),
        ({"refund.velocity_max_refunds": 10}, {"recent_refund_count": 3}, ASK, ALLOW),
        (
            {"refund.refundable_statuses": ["paid", "delivered", "shipped"]},
            {"order_status": OrderStatus.shipped},
            DENY,
            ALLOW,
        ),
        (
            {"refund.auto_approve_reasons": ["damaged", "other"]},
            {"reason_code": "other"},
            ASK,
            ALLOW,
        ),
    ],
)
def test_outcomes_follow_the_rules_not_the_code(
    rule_overrides, fact_overrides, default_outcome, new_outcome
) -> None:
    assert run(**fact_overrides).decision == default_outcome
    assert run(make_rules(**rule_overrides), **fact_overrides).decision == new_outcome


def test_actual_thresholds_in_the_shipped_rules_file() -> None:
    r = make_rules().refund
    assert (r.auto_approve_max, r.max_order_age_days, r.velocity_max_refunds) == (
        D("500.00"),
        30,
        3,
    )
    assert r.currency == "INR"


# --- human approval ----------------------------------------------------------------------


def test_human_approval_waives_escalations_but_never_denials() -> None:
    rules, a = make_rules(), actor(Role.admin)
    needs_review = make_facts(amount=D("900.00"), reason_code="changed_mind", recent_refund_count=5)
    assert evaluate_refund_request(rules, a, needs_review, NOW).decision == ASK
    approved = evaluate_refund_request(rules, a, needs_review, NOW, human_approved=True)
    assert (approved.decision, approved.reason_code) == (ALLOW, ReasonCode.REFUND_APPROVED_BY_ADMIN)

    too_old = make_facts(amount=D("900.00"), order_placed_at=NOW - 31 * DAY)
    assert evaluate_refund_request(rules, a, too_old, NOW, human_approved=True).decision == DENY


# --- admin decisions -----------------------------------------------------------------------


@pytest.mark.parametrize("action", [APPROVE_REFUND, REJECT_REFUND])
def test_customers_cannot_perform_admin_only_actions(action) -> None:
    r = evaluate_refund_decision(
        make_rules(),
        action,
        actor(Role.customer),
        requested_by=uuid.uuid4(),
        refund_facts=make_facts(),
        now=NOW,
    )
    assert (r.decision, r.reason_code) == (DENY, ReasonCode.ACTOR_NOT_ADMIN)


def test_admin_cannot_approve_their_own_request() -> None:
    admin = actor(Role.admin)
    r = evaluate_refund_decision(
        make_rules(),
        APPROVE_REFUND,
        admin,
        requested_by=admin.user_id,
        refund_facts=make_facts(),
        now=NOW,
    )
    assert (r.decision, r.reason_code) == (DENY, ReasonCode.SELF_APPROVAL)


def test_separation_of_duties_is_per_action_and_config_driven() -> None:
    admin = actor(Role.admin)
    own = {"requested_by": admin.user_id, "refund_facts": make_facts(), "now": NOW}
    # rejecting your own request is allowed by default (it is just withdrawing it)...
    assert evaluate_refund_decision(make_rules(), REJECT_REFUND, admin, **own).decision == ALLOW
    # ...but the list lives in rules.yaml, not in code
    strict = make_rules(separation_of_duties_actions=["approve_refund", "reject_refund"])
    assert (
        evaluate_refund_decision(strict, REJECT_REFUND, admin, **own).reason_code
        == ReasonCode.SELF_APPROVAL
    )
    lax = make_rules(separation_of_duties_actions=[])
    assert evaluate_refund_decision(lax, APPROVE_REFUND, admin, **own).decision == ALLOW


def test_another_admin_can_approve_and_reject() -> None:
    requester, approver = uuid.uuid4(), actor(Role.admin)
    for action in (APPROVE_REFUND, REJECT_REFUND):
        r = evaluate_refund_decision(
            make_rules(),
            action,
            approver,
            requested_by=requester,
            refund_facts=make_facts(amount=D("900.00"), reason_code="changed_mind"),
            now=NOW,
        )
        assert (r.decision, r.reason_code) == (ALLOW, ReasonCode.ADMIN_ACTION_ALLOWED)


def test_approval_rechecks_hard_rules_against_current_facts() -> None:
    """The request waited; by approval time the order aged out of the window."""
    r = evaluate_refund_decision(
        make_rules(),
        APPROVE_REFUND,
        actor(Role.admin),
        requested_by=uuid.uuid4(),
        refund_facts=make_facts(order_placed_at=NOW - 31 * DAY),
        now=NOW,
    )
    assert (r.decision, r.reason_code) == (DENY, ReasonCode.ORDER_TOO_OLD)


def test_rejecting_does_not_recheck_hard_rules() -> None:
    r = evaluate_refund_decision(
        make_rules(),
        REJECT_REFUND,
        actor(Role.admin),
        requested_by=uuid.uuid4(),
        refund_facts=make_facts(order_placed_at=NOW - 31 * DAY),
        now=NOW,
    )
    assert r.decision == ALLOW


def test_action_gate_is_none_for_actions_without_restrictions() -> None:
    assert evaluate_action_gate(make_rules(), "get_order", actor()) is None


# --- cancel / ticket / read ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "decision", "code"),
    [
        (OrderStatus.pending, ALLOW, ReasonCode.CANCEL_ALLOWED),
        (OrderStatus.paid, DENY, ReasonCode.CANCEL_REQUIRES_REFUND),
        (OrderStatus.shipped, DENY, ReasonCode.ORDER_NOT_CANCELLABLE),
        (OrderStatus.delivered, DENY, ReasonCode.ORDER_NOT_CANCELLABLE),
        (OrderStatus.cancelled, DENY, ReasonCode.ORDER_ALREADY_CANCELLED),
    ],
)
def test_cancel_order_table(status, decision, code) -> None:
    r = evaluate_cancel_order(make_rules(), actor(), CancelFacts(order_status=status))
    assert (r.decision, r.reason_code) == (decision, code)


def test_cancel_rules_are_config_driven() -> None:
    rules = make_rules(
        **{
            "cancel.cancellable_statuses": ["pending", "paid"],
            "cancel.refund_required_statuses": [],
        }
    )
    r = evaluate_cancel_order(rules, actor(), CancelFacts(order_status=OrderStatus.paid))
    assert r.decision == ALLOW


def test_ticket_and_read_are_allowed() -> None:
    assert evaluate_create_ticket(make_rules(), actor()).reason_code == ReasonCode.TICKET_ALLOWED
    assert evaluate_read(make_rules(), actor()).reason_code == ReasonCode.READ_ALLOWED


# --- purity ---------------------------------------------------------------------------------


def test_engine_requires_timezone_aware_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_refund_request(make_rules(), actor(), make_facts(), NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_refund_request(
            make_rules(), actor(), make_facts(order_placed_at=NOW.replace(tzinfo=None)), NOW
        )


def test_engine_is_deterministic_and_does_not_mutate_inputs() -> None:
    rules, facts = make_rules(), make_facts(amount=D("900.00"))
    first = evaluate_refund_request(rules, actor(), facts, NOW)
    for _ in range(5):
        assert evaluate_refund_request(rules, actor(), facts, NOW) == first
    assert facts == make_facts(amount=D("900.00"))


FORBIDDEN_IMPORT_ROOTS = {
    "sqlalchemy",
    "asyncpg",
    "redis",
    "aiokafka",
    "fastapi",
    "httpx",
    "requests",
    "socket",
    "subprocess",
    "random",
}


@pytest.mark.parametrize("module", ["engine.py", "types.py"])
def test_pure_modules_import_no_io_libraries_and_read_no_clock(module) -> None:
    """engine.py and types.py: no DB/network/clock access. (rules.py reads YAML at startup, by
    design, and is not part of evaluation.)"""
    path = Path(policy_pkg.__file__).parent / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & FORBIDDEN_IMPORT_ROOTS, imported & FORBIDDEN_IMPORT_ROOTS
    source = path.read_text(encoding="utf-8")
    for clock_call in ("datetime.now(", "datetime.utcnow(", "time.time(", "date.today("):
        assert clock_call not in source
