import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.db.models.enums import OrderStatus


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sku: str
    name: str
    quantity: int
    unit_price: Decimal  # serialised as a string ("299.00"), never a float


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    status: OrderStatus
    total_amount: Decimal
    currency: str
    placed_at: datetime
    items: list[OrderItemOut]


class OrderListOut(BaseModel):
    items: list[OrderOut]
    total: int
    limit: int
    offset: int
