"""Input schemas for tools. Every model forbids unknown fields: a model-supplied `user_id`
(or any other field we did not define) makes validation FAIL and is logged, rather than being
silently ignored, so attempts to impersonate another user are visible."""

import uuid
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from app.db.models.enums import TicketPriority

Text200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text5000 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GetOrderInput(ToolInput):
    order_id: uuid.UUID


class ListOrdersInput(ToolInput):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0)] = 0


class CreateTicketInput(ToolInput):
    subject: Text200
    body: Text5000
    order_id: uuid.UUID | None = None
    priority: TicketPriority = TicketPriority.normal


class CancelOrderInput(ToolInput):
    order_id: uuid.UUID


def _reject_float_money(value: Any) -> Any:
    if isinstance(value, bool | float):
        raise ValueError(
            'amount must be a decimal string such as "250.00" (or an integer), never a JSON float'
        )
    return value


# Exact money: a string or integer, > 0, at most 2 decimal places, within NUMERIC(12,2).
Amount = Annotated[
    Decimal,
    BeforeValidator(_reject_float_money),
    Field(gt=0, le=Decimal("9999999999.99"), decimal_places=2),  # fits NUMERIC(12,2)
]
ReasonCodeStr = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,31}$")]
Note500 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class RequestRefundInput(ToolInput):
    """The refund request. There is deliberately no user, key or role field: identity comes from
    the session and the Idempotency-Key from the request header."""

    order_id: uuid.UUID
    amount: Amount
    reason_code: ReasonCodeStr
    note: Note500 | None = None


class DecisionInput(ToolInput):
    approval_id: uuid.UUID
    note: Note500 | None = None


class ApproveRefundInput(DecisionInput):
    pass


class RejectRefundInput(DecisionInput):
    pass
