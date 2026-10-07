"""Input schemas for tools. Every model forbids unknown fields: a model-supplied `user_id`
(or any other field we did not define) makes validation FAIL and is logged, rather than being
silently ignored, so attempts to impersonate another user are visible."""

import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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
