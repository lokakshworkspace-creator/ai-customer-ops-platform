import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.models.enums import MessageRole, str_enum

# Stored-size cap (bytes of the JSON text); the agent truncates before insert, this is the backstop.
MAX_CONTENT_BYTES = 32_768


class Message(UUIDPrimaryKeyMixin, Base):
    """One message of a chat conversation. `content` is a list of provider-neutral blocks:
    ``{"type": "text", "text": ...}``, ``{"type": "tool_use", "id", "name", "input"}`` or
    ``{"type": "tool_result", "tool_use_id", "content", "is_error"}``.

    There is no `conversations` table: a conversation is the set of messages sharing a
    `conversation_id`, which the SERVER generates (a client-supplied id that has no messages is
    a 404). Every message carries `user_id`, so ownership never depends on the client.
    `seq` is a strict total order (created_at ties inside one transaction)."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_id_seq", "conversation_id", "seq"),
        CheckConstraint(f"octet_length(content::text) <= {MAX_CONTENT_BYTES}", name="content_size"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    conversation_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL"), index=True
    )
    role: Mapped[MessageRole] = mapped_column(str_enum(MessageRole, "message_role"))
    content: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
