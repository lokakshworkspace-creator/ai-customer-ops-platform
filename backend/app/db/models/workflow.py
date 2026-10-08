import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import WorkflowStatus, str_enum


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Golden rule 4: FSM state lives in Postgres. `state` + `context` are the checkpoint.

    A chat turn is one run: it belongs to a `conversation_id` and is created for ONE client
    `message_id`. The unique indexes make the database the arbiter of "the same message twice":
    the second insert fails, and the caller replays the stored `response` instead of calling the
    model or any tool again. `UNIQUE (user_id, message_id)` also covers a retry of a FIRST
    message, which has no conversation id yet. At most one run per conversation may be `running`.
    `response` is the reply sent to the client, frozen when the turn ended (`status` may change
    later, e.g. waiting_approval -> completed)."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index(
            "uq_workflow_runs_conversation_id_message_id",
            "conversation_id",
            "message_id",
            unique=True,
        ),
        Index("uq_workflow_runs_user_id_message_id", "user_id", "message_id", unique=True),
        Index(
            "uq_workflow_runs_conversation_running",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        str_enum(WorkflowStatus, "workflow_status"), default=WorkflowStatus.running
    )
    state: Mapped[str] = mapped_column(String(64))
    context: Mapped[dict[str, Any]] = mapped_column(
        server_default=text("'{}'::jsonb"), default=dict
    )
    error: Mapped[str | None] = mapped_column(Text)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    message_id: Mapped[uuid.UUID | None]
    response: Mapped[dict[str, Any] | None]
    completed_at: Mapped[datetime | None]
