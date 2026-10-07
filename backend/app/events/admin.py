"""Operator helpers for the outbox (used by scripts/outbox_admin.py)."""

import uuid
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

State = Literal["pending", "parked", "published", "all"]

_WHERE = {
    "pending": "published_at IS NULL AND parked_at IS NULL",
    "parked": "published_at IS NULL AND parked_at IS NOT NULL",
    "published": "published_at IS NOT NULL",
    "all": "true",
}


async def list_events(session: AsyncSession, state: State, limit: int = 50) -> list[dict]:
    rows = await session.execute(
        text(
            "SELECT id, seq, event_type, aggregate_type, aggregate_id, attempts, parked_at, "
            "next_attempt_at, published_at, left(coalesce(last_error, ''), 120) AS last_error "
            f"FROM outbox_events WHERE {_WHERE[state]} ORDER BY seq LIMIT :limit"
        ),
        {"limit": limit},
    )
    return [dict(r) for r in rows.mappings()]


async def blocked_by_parked(session: AsyncSession) -> list[dict]:
    """Per aggregate with a parked event: how many later events are stuck behind it."""
    rows = await session.execute(
        text(
            """
            SELECT p.aggregate_id, p.id AS parked_event_id, p.event_type,
                   (SELECT count(*) FROM outbox_events later
                     WHERE later.aggregate_id = p.aggregate_id AND later.seq > p.seq
                       AND later.published_at IS NULL) AS blocked_events
            FROM outbox_events p
            WHERE p.published_at IS NULL AND p.parked_at IS NOT NULL
            ORDER BY p.seq
            """
        )
    )
    return [dict(r) for r in rows.mappings()]


async def unpark(
    session: AsyncSession,
    *,
    event_ids: list[uuid.UUID] | None = None,
    aggregate_id: str | None = None,
    all_parked: bool = False,
) -> int:
    """Make parked events eligible again: parked_at cleared, attempts reset, due immediately.
    Exactly one selector must be given. Returns how many rows were un-parked."""
    selectors = [event_ids is not None, aggregate_id is not None, all_parked]
    if sum(selectors) != 1:
        raise ValueError("give exactly one of: event_ids, aggregate_id, all_parked")
    base = (
        "UPDATE outbox_events SET parked_at = NULL, attempts = 0, next_attempt_at = now() "
        "WHERE published_at IS NULL AND parked_at IS NOT NULL"
    )
    if event_ids is not None:
        result = await session.execute(text(base + " AND id = ANY(:ids)"), {"ids": event_ids})
    elif aggregate_id is not None:
        result = await session.execute(
            text(base + " AND aggregate_id = :agg"), {"agg": aggregate_id}
        )
    else:
        result = await session.execute(text(base))
    return result.rowcount or 0
