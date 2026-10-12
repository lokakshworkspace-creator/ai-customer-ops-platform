"""Approval expiry (Phase 6): a refund that waits for a human for too long is not left open forever.

An approval that is still `pending` `approval_ttl_hours` after it was created is EXPIRED by the
sweeper (`python -m app.jobs.sweeper`):

* the approval becomes `expired` (decided_by stays empty: nobody decided; decided_at is when the
  sweeper expired it; expires_at records the deadline it passed);
* the refund becomes `expired`. It never moved money. `expired` is not in `reserving_statuses` or
  `velocity_counted_statuses` of rules.yaml, so the money it reserved against the order and its slot
  in the customer's velocity count are released without any change to the policy engine;
* a `refund.expired` event is written to the outbox in the SAME transaction (golden rule 7), and the
  resume consumer finishes the customer's waiting chat turn with "your request expired";
* an `audit_logs` row says what happened (actor: none, it was the system).

The deadline is `created_at + approval_ttl_hours`. The TTL is a setting (APPROVAL_TTL_HOURS), not a
rule in rules.yaml.

TWO PROTECTIONS AGAINST DECIDING AN EXPIRED APPROVAL. (1) Once the sweeper has expired it, its
status is `expired` and the decision handlers refuse it. (2) Between the deadline and the next
sweep it is still `pending`, so the decision handlers also compare `ctx.now` with the deadline
(`is_past_deadline`) and refuse it with APPROVAL_EXPIRED: there is no window in which a late
decision slips through. Both approve and reject are refused, so a late rejection cannot race the
sweeper either; the approval simply expires.

Concurrency: `expire_one` takes the locks in the documented order (order -> user -> refund ->
approval) and RE-CHECKS everything under them, exactly like the approve and reject handlers, so a
sweeper racing an admin's decision (or a second sweeper) can never expire a decided approval or
decide an expired one: whoever gets the locks first wins, the other sees the new status and does
nothing. The candidate query takes no locks (holding row locks across many approvals would break
the lock order).
"""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db import locking
from app.db.models import Approval, Refund
from app.db.models.enums import ApprovalStatus, RefundStatus
from app.events.types import EventType
from app.modules import audit
from app.modules.refunds import service as refund_service

log = structlog.get_logger(__name__)


def approval_ttl() -> timedelta:
    return timedelta(hours=get_settings().approval_ttl_hours)


def deadline_of(approval: Approval, ttl: timedelta | None = None) -> datetime:
    return approval.created_at + (ttl if ttl is not None else approval_ttl())


def is_past_deadline(approval: Approval, now: datetime, ttl: timedelta | None = None) -> bool:
    """True if a still-pending approval is older than the limit (the sweeper may not have run)."""
    return approval.status == ApprovalStatus.pending and now >= deadline_of(approval, ttl)


async def due_approval_ids(
    session: AsyncSession, *, now: datetime, limit: int, ttl: timedelta | None = None
) -> list[uuid.UUID]:
    """Oldest first. No locks: `expire_one` re-checks everything under the proper locks."""
    cutoff = now - (ttl if ttl is not None else approval_ttl())
    rows = await session.scalars(
        select(Approval.id)
        .where(
            Approval.status == ApprovalStatus.pending,
            Approval.refund_id.is_not(None),
            Approval.created_at <= cutoff,
        )
        .order_by(Approval.created_at, Approval.id)
        .limit(limit)
    )
    return list(rows)


async def expire_one(
    session: AsyncSession,
    approval_id: uuid.UUID,
    *,
    now: datetime | None = None,
    ttl: timedelta | None = None,
    request_id: str | None = None,
) -> bool:
    """Expire one approval if it is still pending and past its deadline. Commits if it did,
    otherwise rolls back (releasing the locks). Returns True if it expired it."""
    now = now or datetime.now(UTC)
    ttl = ttl if ttl is not None else approval_ttl()

    approval0 = await session.get(Approval, approval_id)
    if approval0 is None or approval0.refund_id is None:
        await session.rollback()
        return False
    refund0 = await session.get(Refund, approval0.refund_id)
    assert refund0 is not None

    # Same order as the decision handlers: order -> user -> refund -> approval.
    order = await locking.lock_order(session, refund0.order_id)
    assert order is not None
    await locking.lock_user(session, order.user_id)
    refund = await locking.lock_refund(session, refund0.id)
    approval = await locking.lock_approval(session, approval_id)
    assert refund is not None and approval is not None
    customer_id = order.user_id

    # Re-checked under the locks: a decision or another sweeper may have got here first.
    if (
        approval.status != ApprovalStatus.pending
        or refund.status != RefundStatus.pending_approval
        or not is_past_deadline(approval, now, ttl)
    ):
        await session.rollback()
        return False

    hours = ttl.total_seconds() / 3600
    approval.status = ApprovalStatus.expired
    approval.decided_at = now
    approval.expires_at = deadline_of(approval, ttl)
    approval.decision_reason = f"Expired: nobody decided within {hours:g} hours."
    refund.status = RefundStatus.expired
    refund.processed_at = now
    await session.flush()
    refund_service.emit(
        session, refund, customer_id, EventType.REFUND_EXPIRED, request_id=request_id
    )
    audit.record(
        session,
        actor_id=None,  # the system, not a person
        action="refund.expired",
        resource_type="refund",
        resource_id=refund.id,
        reason=approval.decision_reason,
        details={"approval_id": approval.id, "ttl_hours": hours},
        request_id=request_id,
    )
    refund_id = refund.id
    await session.commit()
    log.info("approval_expired", approval_id=str(approval_id), refund_id=str(refund_id))
    return True
