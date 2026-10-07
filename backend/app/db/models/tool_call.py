import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.models.enums import PolicyDecision, ToolCallStatus, str_enum


class ToolCall(UUIDPrimaryKeyMixin, Base):
    """Golden rule 2: every tool call, allowed or not, is logged here."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_workflow_run_id_created_at", "workflow_run_id", "created_at"),
    )

    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[dict[str, Any]] = mapped_column(
        server_default=text("'{}'::jsonb"), default=dict
    )
    status: Mapped[ToolCallStatus] = mapped_column(
        str_enum(ToolCallStatus, "tool_call_status"), default=ToolCallStatus.pending
    )
    policy_decision: Mapped[PolicyDecision | None] = mapped_column(
        str_enum(PolicyDecision, "policy_decision")
    )
    policy_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
