"""Order queries.

`user_id` is the ownership filter: pass a user id to restrict results to that user's orders,
or None for an unrestricted (admin) query. The service layer decides which one to use.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Order


async def list_orders(
    session: AsyncSession, *, user_id: uuid.UUID | None, limit: int, offset: int
) -> tuple[list[Order], int]:
    filters = [] if user_id is None else [Order.user_id == user_id]

    total = await session.scalar(select(func.count()).select_from(Order).where(*filters))
    rows = await session.scalars(
        select(Order)
        .where(*filters)
        .options(selectinload(Order.items))
        .order_by(Order.placed_at.desc(), Order.id.desc())  # id: stable tie-break
        .limit(limit)
        .offset(offset)
    )
    return list(rows), total or 0


async def get_order(
    session: AsyncSession,
    order_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
) -> Order | None:
    """Read an order (never locks: mutating flows use app.db.locking.lock_order)."""
    stmt = select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    return await session.scalar(stmt)
