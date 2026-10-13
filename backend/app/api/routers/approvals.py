"""Admin endpoints for approvals: the list (what the admin page shows) and approve / reject.

The list is admin-only (403 for customers) and read-only. Approve and reject go through the SAME
tool executor as everything else (authorization, the policy engine with its SELF_APPROVAL rule,
locks, the tool_calls log); this module and the admin page decide nothing."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_executor, get_session, require_role
from app.api.tool_responses import tool_response
from app.db.models import User
from app.db.models.enums import ApprovalStatus, Role
from app.modules.refunds import approvals_view
from app.policy.rules import get_rules
from app.policy.types import Actor
from app.tools.context import ToolContext
from app.tools.executor import ToolExecutor
from app.tools.schemas import Note500

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Note500 | None = None


@router.get("")
async def list_approvals(
    _admin: Annotated[User, Depends(require_role(Role.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: ApprovalStatus = ApprovalStatus.pending,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    rows, total = await approvals_view.list_for_admin(
        session, status=status, limit=limit, offset=offset
    )
    return {
        "items": await approvals_view.admin_items(session, rows, get_rules()),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def _decide(
    tool: str,
    approval_id: uuid.UUID,
    body: DecisionBody | None,
    request: Request,
    user: User,
    session: AsyncSession,
    executor: ToolExecutor,
) -> JSONResponse:
    # Plain data first: the executor's rollbacks expire ORM objects such as `user`.
    actor = Actor(id=user.id, role=user.role)
    ctx = ToolContext(actor=actor, request_id=getattr(request.state, "request_id", None))
    args = {"approval_id": str(approval_id), "note": body.note if body else None}
    # Not pre-gated by require_role on purpose: a customer's attempt must reach the executor so
    # the denial is logged (tool_calls + audit_logs) and answered with 403.
    return tool_response(await executor.run(session, ctx, tool, args))


@router.post("/{approval_id}/approve")
async def approve(
    approval_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    executor: Annotated[ToolExecutor, Depends(get_executor)],
    body: DecisionBody | None = None,
) -> JSONResponse:
    return await _decide("approve_refund", approval_id, body, request, user, session, executor)


@router.post("/{approval_id}/reject")
async def reject(
    approval_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    executor: Annotated[ToolExecutor, Depends(get_executor)],
    body: DecisionBody | None = None,
) -> JSONResponse:
    return await _decide("reject_refund", approval_id, body, request, user, session, executor)
