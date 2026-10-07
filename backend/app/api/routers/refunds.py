import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_executor, get_session
from app.api.tool_responses import tool_response
from app.modules.orders import service as orders_service
from app.modules.refunds import repo as refunds_repo
from app.modules.refunds.hashing import IDEMPOTENCY_KEY_PATTERN
from app.modules.refunds.service import refund_json
from app.policy.types import Actor
from app.tools.context import ToolContext
from app.tools.executor import ToolExecutor
from app.tools.schemas import RequestRefundInput

router = APIRouter(prefix="/api/v1/refunds", tags=["refunds"])

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=255,
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description=(
            "Required. 8-255 characters of `A-Z a-z 0-9 . _ : -`. Unique per user: repeating a "
            "request with the same key and the same payload returns the original result "
            "(`Idempotent-Replayed: true`) and never creates a second refund; the same key with "
            "a different payload is a 409."
        ),
    ),
]

CREATE_DESCRIPTION = """
Request a refund for one of your orders. **Policy decides** (rules live in `rules.yaml`):

| Outcome | HTTP | Body |
|---|---|---|
| Within limits: paid out at once | **201** | `refund.status` is `succeeded`, or `failed` if the payment provider declined |
| Needs a human (over the automatic limit, 4th refund in 30 days, reason needing review, ...) | **202** | `refund.status = pending_approval`, plus an `approval_id` |
| Payout outcome unknown (provider timeout) | **202** | `refund.status = processing`; it is retried automatically |
| Same `Idempotency-Key` + same payload again | **200** | the original refund, header `Idempotent-Replayed: true` |
| Same `Idempotency-Key`, different payload | **409** | `IDEMPOTENCY_KEY_REUSED` |
| Forbidden by policy (too old, over the order total, not refundable, ...) | **403** | `code` and a human `detail`; nothing is created |
| Order missing **or not yours** | **404** | identical for both |

**A declined payout is a 201, not an error:** the request was accepted and recorded, so a refund
exists with `refund.status = "failed"` (and an `error` object). `failed` is terminal for that
Idempotency-Key: repeating the key returns the same failed refund without contacting the provider
again. To try again, send a **new** Idempotency-Key.

`amount` must be a decimal **string** (or integer) such as `"250.00"`, never a JSON float.
"""


@router.post("", status_code=status.HTTP_201_CREATED, description=CREATE_DESCRIPTION)
async def create_refund(
    request: Request,
    body: RequestRefundInput,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    executor: Annotated[ToolExecutor, Depends(get_executor)],
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    # Copy what we need out of the ORM user NOW. The executor commits and rolls back on this
    # session, and a rollback expires every loaded object: `user` must not be touched below.
    actor = Actor(id=user.id, role=user.role)
    ctx = ToolContext(
        actor=actor,
        request_id=getattr(request.state, "request_id", None),
        idempotency_key=idempotency_key,
    )
    outcome = await executor.run(session, ctx, "request_refund", body.model_dump(mode="json"))
    return tool_response(outcome)


@router.get("/{refund_id}")
async def get_refund(
    refund_id: uuid.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """A refund on one of your orders (admins: any). Anything else is a 404."""
    actor = Actor(id=user.id, role=user.role)
    refund = await refunds_repo.get_scoped(
        session, refund_id, customer_id=orders_service.owner_filter(actor)
    )
    if refund is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Refund not found")
    return {"refund": refund_json(refund)}
