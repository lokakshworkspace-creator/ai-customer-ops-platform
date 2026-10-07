import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.models.enums import PolicyDecision, ToolCallStatus, str_enum

# Stored-size caps (bytes of the JSON text). The executor truncates before insert; these
# CHECK constraints are the backstop, so a bug or a huge model-supplied argument can never
# bloat the table. Keep in sync with migration 0002.
MAX_ARGUMENTS_BYTES = 16_384
MAX_RESULT_BYTES = 32_768


class ToolCall(UUIDPrimaryKeyMixin, Base):
    """Golden rule 2: every tool call, allowed or not, is logged here."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_workflow_run_id_created_at", "workflow_run_id", "created_at"),
        CheckConstraint(
            f"octet_length(arguments::text) <= {MAX_ARGUMENTS_BYTES}", name="arguments_size"
        ),
        CheckConstraint(
            f"result IS NULL OR octet_length(result::text) <= {MAX_RESULT_BYTES}",
            name="result_size",
        ),
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
    policy_reason_code: Mapped[str | None] = mapped_column(String(64))  # machine-readable
    policy_reason: Mapped[str | None] = mapped_column(Text)  # human-readable
    result: Mapped[dict[str, Any] | None]
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
