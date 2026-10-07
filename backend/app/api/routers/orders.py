import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_session
from app.modules.orders import service
from app.modules.orders.schemas import OrderListOut, OrderOut

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@router.get("", response_model=OrderListOut)
async def list_orders(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderListOut:
    orders, total = await service.list_orders(session, user, limit=limit, offset=offset)
    return OrderListOut(
        items=[OrderOut.model_validate(o) for o in orders],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: uuid.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OrderOut:
    order = await service.get_order(session, user, order_id)
    if order is None:
        # Same response whether the order doesn't exist or belongs to someone else, so
        # order ids can't be probed.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return OrderOut.model_validate(order)
