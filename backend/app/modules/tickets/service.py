import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket
from app.db.models.enums import TicketPriority
from app.events.outbox import add_event
from app.events.types import AggregateType, EventType


async def create_ticket(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    subject: str,
    body: str,
    order_id: uuid.UUID | None = None,
    priority: TicketPriority = TicketPriority.normal,
    request_id: str | None = None,
) -> Ticket:
    """Insert the ticket and its `ticket.created` outbox event in the caller's transaction."""
    ticket = Ticket(
        user_id=user_id, order_id=order_id, subject=subject, body=body, priority=priority
    )
    session.add(ticket)
    await session.flush()
    add_event(
        session,
        aggregate_type=AggregateType.TICKET,
        aggregate_id=ticket.id,
        event_type=EventType.TICKET_CREATED,
        data={
            "ticket_id": ticket.id,
            "user_id": user_id,
            "order_id": order_id,
            "priority": priority.value,
        },
        request_id=request_id,
    )
    return ticket
