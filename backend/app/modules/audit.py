"""Domain audit rows (who did what to which resource, and what policy decided).

The tool executor writes one audit row per tool call; services add rows for state transitions
(refund approved, completed, ...). `record` only adds to the CALLER'S session: it commits with
the surrounding transaction.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jsonutil import to_json_safe
from app.db.models import AuditLog
from app.db.models.enums import PolicyDecision


def record(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | str,
    decision: PolicyDecision | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_user_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        decision=decision,
        reason_code=reason_code,
        reason=reason,
        details=to_json_safe(details or {}),
        request_id=request_id,
    )
    session.add(row)
    return row
