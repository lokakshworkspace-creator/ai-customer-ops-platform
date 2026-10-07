"""The policy engine: pure functions from (rules, actor, facts, now) to a decision.

No database, no network, no clock: callers load the facts and pass `now`. Every threshold comes
from `Rules` (rules.yaml). Precedence is fixed: any DENY wins over any REQUIRE_APPROVAL, which
wins over ALLOW. Within a class, the first rule in the order below is the primary reason, and
all rules of that class that fired are listed in `all_reason_codes` for the audit log.
"""

import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.policy.rules import Rules
from app.policy.types import (
    Actor,
    CancelFacts,
    PolicyDecision,
    PolicyResult,
    ReasonCode,
    RefundFacts,
    Role,
)

Finding = tuple[ReasonCode, str]

APPROVE_REFUND = "approve_refund"
REJECT_REFUND = "reject_refund"


def _m(amount: Decimal) -> str:
    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def _result(decision: PolicyDecision, findings: list[Finding], **details: str) -> PolicyResult:
    code, reason = findings[0]
    return PolicyResult(
        decision=decision,
        reason_code=code,
        reason=reason,
        all_reason_codes=tuple(c for c, _ in findings),
        details=details,
    )


# --- refunds -----------------------------------------------------------------------------


def evaluate_refund_request(
    rules: Rules,
    actor: Actor,  # kept in the signature: rules may become actor-dependent
    facts: RefundFacts,
    now: datetime,
    *,
    human_approved: bool = False,
) -> PolicyResult:
    """Decide a refund request. With `human_approved=True` (an admin has approved it) the
    escalation rules are skipped but every DENY rule is still enforced."""
    _require_aware(now, "now")
    _require_aware(facts.order_placed_at, "order_placed_at")
    r = rules.refund
    cur = r.currency

    denies: list[Finding] = []
    if facts.amount <= 0:
        denies.append((ReasonCode.INVALID_AMOUNT, "The refund amount must be greater than zero."))
    if facts.reason_code not in r.valid_reasons:
        denies.append(
            (ReasonCode.INVALID_REASON, f"'{facts.reason_code}' is not a recognised refund reason.")
        )
    if facts.order_status not in r.refundable_statuses:
        allowed = ", ".join(sorted(s.value for s in r.refundable_statuses))
        denies.append(
            (
                ReasonCode.ORDER_NOT_REFUNDABLE,
                f"Orders with status '{facts.order_status.value}' cannot be refunded "
                f"(refundable statuses: {allowed}).",
            )
        )
    if not facts.has_succeeded_payment:
        denies.append((ReasonCode.PAYMENT_NOT_FOUND, "There is no successful payment to refund."))
    age = now - facts.order_placed_at
    if age > timedelta(days=r.max_order_age_days):
        denies.append(
            (
                ReasonCode.ORDER_TOO_OLD,
                f"The order is older than the {r.max_order_age_days}-day refund window.",
            )
        )
    remaining = max(facts.order_total - facts.already_refunded, Decimal("0"))
    if facts.amount > facts.order_total:
        denies.append(
            (
                ReasonCode.AMOUNT_EXCEEDS_ORDER,
                f"The refund of {cur} {_m(facts.amount)} exceeds the order total of "
                f"{cur} {_m(facts.order_total)}.",
            )
        )
    elif facts.amount > remaining:
        denies.append(
            (
                ReasonCode.AMOUNT_EXCEEDS_REMAINING,
                f"The refund of {cur} {_m(facts.amount)} exceeds the {cur} {_m(remaining)} "
                "still refundable on this order.",
            )
        )

    ctx = {
        "amount": _m(facts.amount),
        "order_total": _m(facts.order_total),
        "already_refunded": _m(facts.already_refunded),
        "order_age_days": f"{age.total_seconds() / 86400:.2f}",
        "order_status": facts.order_status.value,
    }
    if denies:
        return _result(PolicyDecision.DENY, denies, **ctx)

    if not human_approved:
        escalations: list[Finding] = []
        if facts.order_currency != cur:
            escalations.append(
                (
                    ReasonCode.CURRENCY_UNSUPPORTED,
                    f"The order is in {facts.order_currency}; automatic limits are defined "
                    f"in {cur}, so a human must review it.",
                )
            )
        if facts.amount > r.auto_approve_max:
            escalations.append(
                (
                    ReasonCode.REFUND_ABOVE_AUTO_LIMIT,
                    f"A refund of {cur} {_m(facts.amount)} is above the automatic limit of "
                    f"{cur} {_m(r.auto_approve_max)} and needs approval.",
                )
            )
        if facts.recent_refund_count + 1 > r.velocity_max_refunds:
            escalations.append(
                (
                    ReasonCode.REFUND_VELOCITY,
                    f"This would be refund number {facts.recent_refund_count + 1} within "
                    f"{r.velocity_window_days} days (limit {r.velocity_max_refunds}); it needs "
                    "approval.",
                )
            )
        if facts.reason_code not in r.auto_approve_reasons:
            escalations.append(
                (
                    ReasonCode.REASON_NEEDS_REVIEW,
                    f"Refunds for '{facts.reason_code}' are not auto-approved and need review.",
                )
            )
        if escalations:
            return _result(PolicyDecision.REQUIRE_APPROVAL, escalations, **ctx)

    if human_approved:
        return _result(
            PolicyDecision.ALLOW,
            [(ReasonCode.REFUND_APPROVED_BY_ADMIN, "Approved by an administrator.")],
            **ctx,
        )
    return _result(
        PolicyDecision.ALLOW,
        [
            (
                ReasonCode.REFUND_ALLOWED,
                f"Refund of {cur} {_m(facts.amount)} is within the automatic limit.",
            )
        ],
        **ctx,
    )


# --- admin decisions (approve / reject) ----------------------------------------------------


def evaluate_action_gate(
    rules: Rules,
    action: str,
    actor: Actor,
    *,
    requested_by: uuid.UUID | None = None,
) -> PolicyResult | None:
    """Role and separation-of-duties rules for an action. Returns a DENY, or None if the gate
    passes. Both lists come from rules.yaml."""
    if action in rules.admin_only_actions and actor.role != Role.admin:
        return PolicyResult(
            PolicyDecision.DENY,
            ReasonCode.ACTOR_NOT_ADMIN,
            f"'{action}' is restricted to administrators.",
            (ReasonCode.ACTOR_NOT_ADMIN,),
            {"action": action, "actor_role": actor.role.value},
        )
    if (
        action in rules.separation_of_duties_actions
        and requested_by is not None
        and requested_by == actor.id
    ):
        return PolicyResult(
            PolicyDecision.DENY,
            ReasonCode.SELF_APPROVAL,
            "You cannot decide a request that you made yourself.",
            (ReasonCode.SELF_APPROVAL,),
            {"action": action},
        )
    return None


def evaluate_refund_decision(
    rules: Rules,
    action: str,
    actor: Actor,
    *,
    requested_by: uuid.UUID,
    refund_facts: RefundFacts,
    now: datetime,
) -> PolicyResult:
    """May `actor` approve or reject this pending refund?

    Approving also re-checks the hard (DENY) rules against current facts, because time passes
    while a request waits: it may have aged out of the refund window, for instance.
    """
    gate = evaluate_action_gate(rules, action, actor, requested_by=requested_by)
    if gate is not None:
        return gate
    if action == APPROVE_REFUND:
        recheck = evaluate_refund_request(rules, actor, refund_facts, now, human_approved=True)
        if recheck.is_deny:
            return recheck
    return PolicyResult(
        PolicyDecision.ALLOW,
        ReasonCode.ADMIN_ACTION_ALLOWED,
        f"'{action}' is permitted.",
        (ReasonCode.ADMIN_ACTION_ALLOWED,),
        {"action": action},
    )


# --- cancel / ticket / read ----------------------------------------------------------------


def evaluate_cancel_order(rules: Rules, actor: Actor, facts: CancelFacts) -> PolicyResult:
    c = rules.cancel
    status = facts.order_status
    details = {"order_status": status.value}
    if status in c.cancellable_statuses:
        return PolicyResult(
            PolicyDecision.ALLOW,
            ReasonCode.CANCEL_ALLOWED,
            "The order has not been paid and can be cancelled.",
            (ReasonCode.CANCEL_ALLOWED,),
            details,
        )
    if status.value == "cancelled":
        code, text = ReasonCode.ORDER_ALREADY_CANCELLED, "The order is already cancelled."
    elif status in c.refund_required_statuses:
        code, text = (
            ReasonCode.CANCEL_REQUIRES_REFUND,
            "This order has been paid; request a refund instead of cancelling it.",
        )
    else:
        code, text = (
            ReasonCode.ORDER_NOT_CANCELLABLE,
            f"Orders with status '{status.value}' cannot be cancelled.",
        )
    return PolicyResult(PolicyDecision.DENY, code, text, (code,), details)


def evaluate_create_ticket(rules: Rules, actor: Actor) -> PolicyResult:
    return PolicyResult(
        PolicyDecision.ALLOW,
        ReasonCode.TICKET_ALLOWED,
        "Creating a support ticket is always permitted.",
        (ReasonCode.TICKET_ALLOWED,),
    )


def evaluate_read(rules: Rules, actor: Actor) -> PolicyResult:
    """Read-only tools: object-level scoping (ownership) is enforced by the queries, not here."""
    return PolicyResult(
        PolicyDecision.ALLOW,
        ReasonCode.READ_ALLOWED,
        "Read-only access to the caller's own data.",
        (ReasonCode.READ_ALLOWED,),
    )
