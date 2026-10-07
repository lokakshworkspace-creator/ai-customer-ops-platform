"""Refund business logic shared by the request / approve / reject handlers and the reconciler.

State machine (refunds.status):

    request ──ALLOW──────────────▶ processing ──provider ok──▶ succeeded
       │                               ▲    └──provider declined──▶ failed   (terminal for the key)
       └─REQUIRE_APPROVAL▶ pending_approval ──admin approve──┘
                                   └──admin reject──▶ rejected
    (DENY creates no refund row at all.)
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import locking
from app.db.models import Order, Payment, Refund
from app.db.models.enums import RefundStatus
from app.events.outbox import add_event
from app.events.types import AggregateType, EventType
from app.modules import audit
from app.modules.refunds import repo
from app.modules.refunds.payments import PaymentProvider, ProviderUnavailable, provider_key_for
from app.policy.rules import Rules
from app.policy.types import RefundFacts

log = structlog.get_logger(__name__)


def money(amount: Decimal) -> str:
    return format(amount, "f")


def refund_json(refund: Refund) -> dict[str, Any]:
    return {
        "id": str(refund.id),
        "order_id": str(refund.order_id),
        "amount": money(refund.amount),
        "currency": refund.currency,
        "status": refund.status.value,
        "reason_code": refund.reason_code,
        "note": refund.reason,
        "requested_by": str(refund.requested_by),
        "created_at": refund.created_at.isoformat() if refund.created_at else None,
        "processed_at": refund.processed_at.isoformat() if refund.processed_at else None,
    }


async def build_facts(
    session: AsyncSession,
    rules: Rules,
    order: Order,
    *,
    has_payment: bool,
    amount: Decimal,
    reason_code: str,
    now: datetime,
    exclude_refund_id: uuid.UUID | None = None,
) -> RefundFacts:
    """Load everything the (pure) policy engine needs about this refund. Call it AFTER taking
    the order and user locks, so the numbers cannot change under the decision."""
    r = rules.refund
    reserved = await repo.reserved_amount(
        session, order.id, r.reserving_statuses, exclude_refund_id=exclude_refund_id
    )
    recent = await repo.recent_count(
        session,
        order.user_id,  # the CUSTOMER's refunds, whoever filed them
        r.velocity_counted_statuses,
        since=now - timedelta(days=r.velocity_window_days),
        exclude_refund_id=exclude_refund_id,
    )
    return RefundFacts(
        amount=amount,
        reason_code=reason_code,
        order_status=order.status,
        order_total=order.total_amount,
        order_currency=order.currency,
        order_placed_at=order.placed_at,
        has_succeeded_payment=has_payment,
        already_refunded=reserved,
        recent_refund_count=recent,
    )


def refund_event_data(refund: Refund, customer_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "refund_id": refund.id,
        "order_id": refund.order_id,
        "customer_id": customer_id,
        "requested_by": refund.requested_by,
        "amount": money(refund.amount),
        "currency": refund.currency,
        "reason_code": refund.reason_code,
        "status": refund.status.value,
        **extra,
    }


def emit(
    session: AsyncSession,
    refund: Refund,
    customer_id: uuid.UUID,
    event_type: EventType,
    *,
    request_id: str | None,
    **extra: Any,
) -> None:
    """Add a refund event to the outbox IN THE CALLER'S TRANSACTION (rule 7)."""
    add_event(
        session,
        aggregate_type=AggregateType.REFUND,
        aggregate_id=refund.id,
        event_type=event_type,
        data=refund_event_data(refund, customer_id, **extra),
        request_id=request_id,
    )


# --- payout ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PayoutOutcome:
    kind: Literal["succeeded", "failed", "unconfirmed", "already_final"]
    refund: dict[str, Any]
    error: str | None = None


async def payout_flow(
    session: AsyncSession,
    provider: PaymentProvider,
    refund_id: uuid.UUID,
    *,
    request_id: str | None,
) -> PayoutOutcome:
    """Pay a `processing` refund out through the provider and record the result.

    Three phases, and the point of the structure is that the provider call (B) happens with NO
    open transaction and NO row locks, so a slow or hung provider can never block anyone else:

      A. short read-only transaction to snapshot what the provider needs, then COMMIT it
      B. provider call, with the idempotency key `refund-<refund id>`
      C. new transaction: lock the refund, re-check it is still `processing`, record the result
         and its outbox event atomically

    Safe to run more than once for the same refund (retry after a crash, or the reconciler racing
    a live request): the provider returns the same result for the same key, and C does nothing if
    another run already finished the refund.
    """
    # A -----------------------------------------------------------------------------------
    refund = await session.get(Refund, refund_id, populate_existing=True)
    if refund is None:
        raise LookupError(f"refund {refund_id} not found")
    snapshot = refund_json(refund)
    if refund.status != RefundStatus.processing:
        await session.commit()
        return PayoutOutcome("already_final", snapshot)
    payment = await session.get(Payment, refund.payment_id)
    payment_ref = payment.provider_ref if payment else None
    amount, currency = refund.amount, refund.currency
    await session.commit()  # nothing stays open across the provider call

    # B -----------------------------------------------------------------------------------
    try:
        result = await provider.refund(
            payment_ref=payment_ref,
            amount=amount,
            currency=currency,
            idempotency_key=provider_key_for(refund_id),
        )
    except ProviderUnavailable as exc:
        log.warning("payout_unconfirmed", refund_id=str(refund_id), error=str(exc))
        return PayoutOutcome("unconfirmed", snapshot, str(exc))

    # C -----------------------------------------------------------------------------------
    refund = await locking.lock_refund(session, refund_id)
    assert refund is not None
    if refund.status != RefundStatus.processing:  # someone else finished it first
        final = refund_json(refund)
        await session.commit()
        return PayoutOutcome("already_final", final)

    order = await session.get(Order, refund.order_id)
    customer_id = order.user_id if order else refund.requested_by
    refund.processed_at = datetime.now(UTC)
    if result.status == "succeeded":
        refund.status = RefundStatus.succeeded
        emit(
            session,
            refund,
            customer_id,
            EventType.REFUND_COMPLETED,
            request_id=request_id,
            provider_ref=result.provider_ref,
        )
        audit.record(
            session,
            actor_id=None,
            action="refund.completed",
            resource_type="refund",
            resource_id=refund.id,
            details={"provider_ref": result.provider_ref},
            request_id=request_id,
        )
    else:
        refund.status = RefundStatus.failed
        emit(
            session,
            refund,
            customer_id,
            EventType.REFUND_FAILED,
            request_id=request_id,
            error=result.error,
        )
        audit.record(
            session,
            actor_id=None,
            action="refund.failed",
            resource_type="refund",
            resource_id=refund.id,
            reason=result.error,
            request_id=request_id,
        )
    await session.flush()
    final = refund_json(refund)
    await session.commit()
    return PayoutOutcome(
        "succeeded" if result.status == "succeeded" else "failed", final, result.error
    )
