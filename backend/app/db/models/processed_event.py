import uuid
from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessedEvent(Base):
    """Consumer-side dedupe. Events are delivered at least once, so each consumer records
    (consumer, event_id) in the same transaction as its effect; a duplicate delivery hits the
    primary key and is skipped. Composite natural key on purpose: this is a dedupe ledger, not
    an entity."""

    __tablename__ = "processed_events"

    consumer: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(server_default=func.now())
