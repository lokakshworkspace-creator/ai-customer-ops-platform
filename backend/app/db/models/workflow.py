import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import WorkflowStatus, str_enum


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Golden rule 4: FSM state lives in Postgres. `state` + `context` are the checkpoint."""

    __tablename__ = "workflow_runs"

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
    completed_at: Mapped[datetime | None]
