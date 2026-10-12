"""What the admin approvals page shows for each approval. Read-only; it decides nothing.

Everything comes from rows that already exist: the approval and its payload (the amount, the reason
and the policy reasons that sent it to review were written there when the refund was requested), the
refund, the order, and a count of the customer's refunds in the policy's velocity window (the same
query the policy engine uses). No emails or names: the requester is shown by id only.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, Order, Refund
from app.db.models.enums import ApprovalStatus
from app.modules.refunds import expiry, repo
from app.modules.refunds.service import money
from app.policy.rules import Rules


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def admin_items(
    session: AsyncSession,
    approvals: Sequence[Approval],
    rules: Rules,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    refund_ids = [a.refund_id for a in approvals if a.refund_id]
    refunds: dict[uuid.UUID, Refund] = {}
    orders: dict[uuid.UUID, Order] = {}
    if refund_ids:
        refunds = {
            r.id: r for r in await session.scalars(select(Refund).where(Refund.id.in_(refund_ids)))
        }
        order_ids = {r.order_id for r in refunds.values()}
        orders = {
            o.id: o for o in await session.scalars(select(Order).where(Order.id.in_(order_ids)))
        }
    ttl = expiry.approval_ttl()
    window = rules.refund.velocity_window_days
    since = now - timedelta(days=window)

    items: list[dict[str, Any]] = []
    for a in approvals:
        refund = refunds.get(a.refund_id) if a.refund_id else None
        order = orders.get(refund.order_id) if refund else None
        in_window = (
            await repo.recent_count(
                session, order.user_id, rules.refund.velocity_counted_statuses, since=since
            )
            if order
            else None
        )
        items.append(
            {
                "id": str(a.id),
                "refund_id": str(a.refund_id) if a.refund_id else None,
                "status": a.status.value,
                "requested_by": str(a.requested_by),
                "created_at": a.created_at.isoformat(),
                "decided_by": str(a.decided_by) if a.decided_by else None,
                "decided_at": _iso(a.decided_at),
                "decision_reason": a.decision_reason,
                # the deadline: stored when the sweeper expired it, else created_at + the limit
                # for a pending one (a decided approval has no deadline to show)
                "expires_at": _iso(
                    a.expires_at
                    or (expiry.deadline_of(a, ttl) if a.status == ApprovalStatus.pending else None)
                ),
                "payload": a.payload,
                "refund": (
                    {
                        "amount": money(refund.amount),
                        "currency": refund.currency,
                        "reason_code": refund.reason_code,
                        "status": refund.status.value,
                        "order_id": str(refund.order_id),
                    }
                    if refund
                    else None
                ),
                "order": (
                    {
                        "status": order.status.value,
                        "total_amount": money(order.total_amount),
                        "currency": order.currency,
                        "placed_at": order.placed_at.isoformat(),
                    }
                    if order
                    else None
                ),
                # why it needs a human, as the policy engine reported it when it was requested
                "review_reasons": list((a.payload or {}).get("reason_codes", [])),
                "review_text": (a.payload or {}).get("reason"),
                # the customer's refunds that count toward the limit, this one included while it
                # is pending
                "requester_refunds_in_window": in_window,
                "velocity_window_days": window,
            }
        )
    return items
