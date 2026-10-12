import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RevokedToken(Base):
    """A token that was logged out (Phase 6). Postgres, not Redis, on purpose: this is security
    state, and Redis is disposable (golden rule 4), so losing it must never un-revoke a token.

    `jti` is the token's unique id. `expires_at` is the token's own expiry: once it has passed the
    token is invalid anyway, so the row is only kept until then and purged by the sweeper. A
    natural primary key, like `processed_events`: this is a ledger, not an entity."""

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("ix_revoked_tokens_expires_at", "expires_at"),)
