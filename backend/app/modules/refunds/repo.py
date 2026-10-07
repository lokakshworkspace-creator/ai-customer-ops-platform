import uuid
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, Order, Payment, Refund
from app.db.models.enums import ApprovalStatus, PaymentStatus, RefundStatus


async def get_by_user_key(
    session: AsyncSession, requested_by: uuid.UUID, idempotency_key: str
) -> Refund | None:
    stmt = (
        select(Refund)
        .where(Refund.requested_by == requested_by, Refund.idempotency_key == idempotency_key)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def succeeded_payment(session: AsyncSession, order_id: uuid.UUID) -> Payment | None:
    return await session.scalar(
        select(Payment)
        .where(Payment.order_id == order_id, Payment.status == PaymentStatus.succeeded)
        .order_by(Payment.created_at.desc(), Payment.id)
        .limit(1)
    )


async def reserved_amount(
    session: AsyncSession,
    order_id: uuid.UUID,
    statuses: Collection[RefundStatus],
    *,
    exclude_refund_id: uuid.UUID | None = None,
) -> Decimal:
    """Money already committed against this order by refunds in `statuses`."""
    stmt = select(func.coalesce(func.sum(Refund.amount), 0)).where(
        Refund.order_id == order_id, Refund.status.in_(list(statuses))
    )
    if exclude_refund_id is not None:
        stmt = stmt.where(Refund.id != exclude_refund_id)
    return Decimal(await session.scalar(stmt) or 0)


async def recent_count(
    session: AsyncSession,
    customer_id: uuid.UUID,
    statuses: Collection[RefundStatus],
    *,
    since: datetime,
    exclude_refund_id: uuid.UUID | None = None,
) -> int:
    """Refunds on the CUSTOMER's orders (whoever requested them) created at or after `since`."""
    stmt = (
        select(func.count())
        .select_from(Refund)
        .join(Order, Order.id == Refund.order_id)
        .where(
            Order.user_id == customer_id,
            Refund.status.in_(list(statuses)),
            Refund.created_at >= since,
        )
    )
    if exclude_refund_id is not None:
        stmt = stmt.where(Refund.id != exclude_refund_id)
    return int(await session.scalar(stmt) or 0)


async def insert_idempotent(session: AsyncSession, values: dict[str, Any]) -> uuid.UUID | None:
    """INSERT ... ON CONFLICT DO NOTHING RETURNING id.

    The database's UNIQUE (requested_by, idempotency_key) is the arbiter: it returns None when
    that key already exists for this user (a concurrent request won), instead of raising. A
    conflicting insert WAITS for the other transaction to finish, so by the time this returns
    None the winner's row is committed and visible to the next statement.
    """
    stmt = pg_insert(Refund).values(**values).on_conflict_do_nothing().returning(Refund.id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_scoped(
    session: AsyncSession, refund_id: uuid.UUID, *, customer_id: uuid.UUID | None
) -> Refund | None:
    """A refund visible to a customer = one on THEIR order. customer_id=None (admin) sees all."""
    stmt = select(Refund).where(Refund.id == refund_id).execution_options(populate_existing=True)
    if customer_id is not None:
        stmt = stmt.join(Order, Order.id == Refund.order_id).where(Order.user_id == customer_id)
    return await session.scalar(stmt)


async def latest_approval_for(session: AsyncSession, refund_id: uuid.UUID) -> Approval | None:
    return await session.scalar(
        select(Approval)
        .where(Approval.refund_id == refund_id)
        .order_by(Approval.created_at.desc(), Approval.id)
        .limit(1)
    )


async def list_approvals(
    session: AsyncSession, *, status: ApprovalStatus, limit: int, offset: int
) -> tuple[list[Approval], int]:
    total = await session.scalar(
        select(func.count()).select_from(Approval).where(Approval.status == status)
    )
    rows = await session.scalars(
        select(Approval)
        .where(Approval.status == status)
        .order_by(Approval.created_at, Approval.id)
        .limit(limit)
        .offset(offset)
    )
    return list(rows), int(total or 0)
