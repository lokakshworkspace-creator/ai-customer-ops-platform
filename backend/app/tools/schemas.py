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
    order_id: uuid.UUID = Field(
        description="The id (UUID) of the order, as returned by list_orders."
    )


class ListOrdersInput(ToolInput):
    limit: Annotated[int, Field(ge=1, le=100, description="How many orders to return.")] = 20
    offset: Annotated[int, Field(ge=0, description="How many orders to skip.")] = 0


class CreateTicketInput(ToolInput):
    subject: Text200 = Field(description="A short summary of the problem.")
    body: Text5000 = Field(description="What happened, in the customer's own words.")
    order_id: uuid.UUID | None = Field(None, description="The order this is about, if any.")
    priority: TicketPriority = Field(TicketPriority.normal, description="low, normal or high.")


class SearchKnowledgeBaseInput(ToolInput):
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=500)
    ] = Field(description="The customer's question about shop policies or FAQs, in plain words.")


class CancelOrderInput(ToolInput):
    order_id: uuid.UUID = Field(description="The id (UUID) of the unpaid order to cancel.")


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

    order_id: uuid.UUID = Field(description="The id (UUID) of the order to refund.")
    amount: Amount = Field(description="Amount to refund, in the order's currency.")
    reason_code: ReasonCodeStr = Field(description="Why the customer wants a refund.")
    note: Note500 | None = Field(None, description="Optional note, at most 500 characters.")


class DecisionInput(ToolInput):
    approval_id: uuid.UUID
    note: Note500 | None = None


class ApproveRefundInput(DecisionInput):
    pass


class RejectRefundInput(DecisionInput):
    pass
