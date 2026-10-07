"""Golden rule 7: events go through a transactional outbox.

`add_event` only adds a row to the CALLER'S session. It never flushes, commits, or talks to a
broker, so the event is committed (or rolled back) together with the state change that caused
it. A separate relay publishes unpublished rows later.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jsonutil import to_json_safe
from app.db.models import OutboxEvent
from app.events.types import AggregateType, EventType


def add_event(
    session: AsyncSession,
    *,
    aggregate_type: AggregateType | str,
    aggregate_id: uuid.UUID | str,
    event_type: EventType | str,
    data: dict[str, Any],
    request_id: str | None = None,
) -> OutboxEvent:
    event = OutboxEvent(
        aggregate_type=str(aggregate_type),
        aggregate_id=str(aggregate_id),
        event_type=str(event_type),
        payload={"data": to_json_safe(data), "meta": {"request_id": request_id}},
    )
    session.add(event)
    return event
