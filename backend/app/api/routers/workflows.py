"""GET /api/v1/workflows/{id}: the status of one chat turn (owner-only; 404 for everyone else).

For a turn that is `waiting_approval` this call first runs `resume.sync_waiting`, which finishes the
workflow if a person has decided the approval since. That is a write inside a GET; see the long
comment in app/agent/resume.py for why it is safe (one idempotent, row-locked transition, no model
call, only backend-written ids). Phase 5 replaces this polling with event-driven resume.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import resume, store
from app.api.deps import CurrentUser, get_session
from app.db.models.enums import WorkflowStatus

router = APIRouter(prefix="/api/v1/workflows", tags=["chat"])


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: uuid.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    user_id = user.id  # the session is committed and rolled back below: keep plain values
    run = await store.get_run(session, workflow_id, user_id)
    if run is None:
        # Same answer whether it does not exist or belongs to someone else (even an admin).
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    if run.status == WorkflowStatus.waiting_approval:
        await resume.sync_waiting(session, workflow_id)
        run = await store.get_run(session, workflow_id, user_id)
        assert run is not None
    response = dict(run.response or {})
    texts = await store.assistant_texts(session, workflow_id)
    out = {
        "workflow_id": str(run.id),
        "conversation_id": str(run.conversation_id) if run.conversation_id else None,
        "status": run.status.value,
        "state": run.state,
        "reply": response.get("reply"),  # the original reply, as sent
        "latest_message": texts[-1] if texts else None,  # includes a later "approved / paid" update
        "tool_results": response.get("tool_results", []),
        "llm": response.get("llm"),
        "waiting_for_approval": run.status == WorkflowStatus.waiting_approval,
    }
    await session.rollback()  # end the read transaction
    return out
