from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Identity, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class OutboxEvent(UUIDPrimaryKeyMixin, Base):
    """Golden rule 7: events are written here in the same transaction as the state change;
    a relay publishes them later. Request handlers never publish directly.

    `seq` gives a strict total order (created_at is the transaction start time, so events
    written in one transaction tie). The relay publishes events of one aggregate in `seq`
    order and will not publish an event while an earlier one for the same aggregate is still
    unpublished. A `parked` event (parked_at set) has exhausted its retries: the relay stops
    retrying it, but it is still unpublished, so it BLOCKS later events for that aggregate
    until someone resolves it.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("uq_outbox_events_seq", "seq", unique=True),
        # What the relay scans: due, unpublished, not parked.
        Index(
            "ix_outbox_events_pending",
            "next_attempt_at",
            "seq",
            postgresql_where=text("published_at IS NULL AND parked_at IS NULL"),
        ),
        # Supports the "earlier unpublished event for this aggregate?" check.
        Index(
            "ix_outbox_events_aggregate_unpublished",
            "aggregate_id",
            "seq",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(
        server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(server_default=text("0"), default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    parked_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
