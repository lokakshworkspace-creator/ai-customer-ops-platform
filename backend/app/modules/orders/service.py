import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order
from app.db.models.enums import OrderStatus, Role
from app.modules.orders import repo


class Principal(Protocol):
    """Anything with an id and a role: the ORM `User` or the policy/tool `Actor`."""

    id: uuid.UUID
    role: Role


def owner_filter(actor: Principal) -> uuid.UUID | None:
    """The owner filter for `actor`: None (everything) only for an explicit admin.

    Allow-list, not deny-list: any other or future role sees only its own orders.
    """
    return None if actor.role == Role.admin else actor.id


async def list_orders(
    session: AsyncSession, actor: Principal, *, limit: int, offset: int
) -> tuple[list[Order], int]:
    return await repo.list_orders(session, user_id=owner_filter(actor), limit=limit, offset=offset)


async def get_order(session: AsyncSession, actor: Principal, order_id: uuid.UUID) -> Order | None:
    """None means "not found *for this actor*": nonexistent and not-yours are the same."""
    return await repo.get_order(session, order_id, user_id=owner_filter(actor))


async def cancel_order(session: AsyncSession, order: Order) -> None:
    """Mark the order cancelled. The caller has already locked it and passed the policy gate."""
    order.status = OrderStatus.cancelled
    await session.flush()
