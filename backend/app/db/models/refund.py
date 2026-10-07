import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import RefundStatus, str_enum


class Refund(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Golden rule 6: refunds are idempotent. The database UNIQUE constraint on
    (requested_by, idempotency_key) is the source of truth; any Redis fast path is only an
    optimisation. Keys are scoped per user, so two users may use the same key. `request_hash`
    fingerprints the canonical payload: the same key with a different payload is a conflict."""

    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        UniqueConstraint(
            "requested_by", "idempotency_key", name="uq_refunds_requested_by_idempotency_key"
        ),
    )

    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))  # SHA-256 hex of the canonical payload
    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    # Not CHECK-constrained: the set of reasons and which auto-approve is configuration
    # (policy/rules.yaml), validated by the API schema and the policy engine.
    reason_code: Mapped[str] = mapped_column(String(32))
    status: Mapped[RefundStatus] = mapped_column(str_enum(RefundStatus, "refund_status"))
    reason: Mapped[str | None] = mapped_column(Text)  # optional free-text note
    processed_at: Mapped[datetime | None]
