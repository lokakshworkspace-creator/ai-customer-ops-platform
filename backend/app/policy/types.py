"""Inputs and outputs of the policy engine. Plain frozen dataclasses: the engine never touches
a database, so callers load the facts and pass them in."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

# Only the enum vocabulary is shared with the persistence layer; nothing here does I/O.
from app.db.models.enums import OrderStatus, PolicyDecision, Role

__all__ = [
    "Actor",
    "CancelFacts",
    "OrderStatus",
    "PolicyDecision",
    "PolicyResult",
    "ReasonCode",
    "RefundFacts",
    "Role",
]


class ReasonCode(StrEnum):
    """Machine-readable reason, stored in tool_calls / audit_logs next to the human text."""

    # ALLOW
    REFUND_ALLOWED = "REFUND_ALLOWED"
    REFUND_APPROVED_BY_ADMIN = "REFUND_APPROVED_BY_ADMIN"
    CANCEL_ALLOWED = "CANCEL_ALLOWED"
    TICKET_ALLOWED = "TICKET_ALLOWED"
    READ_ALLOWED = "READ_ALLOWED"
    ADMIN_ACTION_ALLOWED = "ADMIN_ACTION_ALLOWED"
    # REQUIRE_APPROVAL
    REFUND_ABOVE_AUTO_LIMIT = "REFUND_ABOVE_AUTO_LIMIT"
    REFUND_VELOCITY = "REFUND_VELOCITY"
    REASON_NEEDS_REVIEW = "REASON_NEEDS_REVIEW"
    CURRENCY_UNSUPPORTED = "CURRENCY_UNSUPPORTED"
    # DENY
    INVALID_AMOUNT = "INVALID_AMOUNT"
    INVALID_REASON = "INVALID_REASON"
    ORDER_NOT_REFUNDABLE = "ORDER_NOT_REFUNDABLE"
    PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
    ORDER_TOO_OLD = "ORDER_TOO_OLD"
    AMOUNT_EXCEEDS_ORDER = "AMOUNT_EXCEEDS_ORDER"
    AMOUNT_EXCEEDS_REMAINING = "AMOUNT_EXCEEDS_REMAINING"
    ACTOR_NOT_ADMIN = "ACTOR_NOT_ADMIN"
    SELF_APPROVAL = "SELF_APPROVAL"
    ROLE_NOT_PERMITTED = "ROLE_NOT_PERMITTED"  # raised by the executor's authorization stage
    ORDER_ALREADY_CANCELLED = "ORDER_ALREADY_CANCELLED"
    CANCEL_REQUIRES_REFUND = "CANCEL_REQUIRES_REFUND"
    ORDER_NOT_CANCELLABLE = "ORDER_NOT_CANCELLABLE"


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated caller. Always comes from the session, never from tool arguments."""

    id: uuid.UUID
    role: Role


@dataclass(frozen=True, slots=True)
class RefundFacts:
    amount: Decimal
    reason_code: str
    order_status: OrderStatus
    order_total: Decimal
    order_currency: str
    order_placed_at: datetime  # timezone-aware
    has_succeeded_payment: bool
    # Sum of OTHER refunds on this order that still hold money: pending_approval, processing
    # or succeeded (failed/rejected release it). Excludes the refund being evaluated.
    already_refunded: Decimal
    # The customer's other refunds inside the velocity window (same status filter).
    recent_refund_count: int


@dataclass(frozen=True, slots=True)
class CancelFacts:
    order_status: OrderStatus


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: PolicyDecision
    reason_code: ReasonCode  # the primary reason
    reason: str  # human-readable, safe to show the caller
    all_reason_codes: tuple[ReasonCode, ...] = ()  # every rule of the winning class that fired
    details: dict[str, str] = field(default_factory=dict)  # JSON-safe context for the audit log

    @property
    def is_allow(self) -> bool:
        return self.decision == PolicyDecision.ALLOW

    @property
    def is_deny(self) -> bool:
        return self.decision == PolicyDecision.DENY

    @property
    def needs_approval(self) -> bool:
        return self.decision == PolicyDecision.REQUIRE_APPROVAL
