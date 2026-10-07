import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import RefundStatus, str_enum


class Refund(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Golden rule 6: refunds are idempotent. `idempotency_key` UNIQUE NOT NULL is the
    source of truth; any Redis fast path is only an optimisation."""

    __tablename__ = "refunds"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[RefundStatus] = mapped_column(
        str_enum(RefundStatus, "refund_status"), default=RefundStatus.pending
    )
    reason: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None]
