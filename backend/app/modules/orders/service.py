import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, User
from app.db.models.enums import Role
from app.modules.orders import repo


def _visible_to(actor: User) -> uuid.UUID | None:
    """The owner filter for `actor`: None (everything) only for an explicit admin.

    Allow-list, not deny-list: any other or future role sees only its own orders.
    """
    return None if actor.role == Role.admin else actor.id


async def list_orders(
    session: AsyncSession, actor: User, *, limit: int, offset: int
) -> tuple[list[Order], int]:
    return await repo.list_orders(session, user_id=_visible_to(actor), limit=limit, offset=offset)


async def get_order(session: AsyncSession, actor: User, order_id: uuid.UUID) -> Order | None:
    """None means "not found *for this actor*": nonexistent and not-yours are the same."""
    return await repo.get_order(session, order_id, user_id=_visible_to(actor))
