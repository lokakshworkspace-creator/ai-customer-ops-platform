"""Shared enums. Stored as VARCHAR + CHECK constraint (not native PG enums) so that adding a
value is a plain migration rather than ALTER TYPE."""

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


class Role(StrEnum):
    customer = "customer"
    admin = "admin"


class OrderStatus(StrEnum):
    pending = "pending"
    paid = "paid"
    shipped = "shipped"
    delivered = "delivered"
    cancelled = "cancelled"


class PaymentStatus(StrEnum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"


class RefundStatus(StrEnum):
    pending = "pending"
    pending_approval = "pending_approval"
    succeeded = "succeeded"
    failed = "failed"
    rejected = "rejected"


class TicketStatus(StrEnum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


class TicketPriority(StrEnum):
    low = "low"
    normal = "normal"
    high = "high"


class WorkflowStatus(StrEnum):
    running = "running"
    waiting_approval = "waiting_approval"
    completed = "completed"
    failed = "failed"


class ApprovalStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class ToolCallStatus(StrEnum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    denied = "denied"
    awaiting_approval = "awaiting_approval"


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


def str_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """VARCHAR column restricted by a named CHECK constraint, mapped to a Python StrEnum."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        length=32,
    )
