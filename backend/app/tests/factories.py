"""Small async helpers for creating rows in tests."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import Order, OrderItem, Payment, Refund, User
from app.db.models.enums import OrderStatus, PaymentStatus, Role


async def create_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    role: Role = Role.customer,
    password: str | None = None,
    password_hash: str = "not-a-real-hash",
    is_active: bool = True,
) -> User:
    user = User(
        email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password(password) if password else password_hash,
        full_name="Test User",
        role=role,
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    return user


async def create_order(
    session: AsyncSession,
    user: User,
    *,
    total: str = "49.98",
    status: OrderStatus = OrderStatus.paid,
    items: list[tuple[str, int, str]] | None = None,
    placed_at: datetime | None = None,
) -> Order:
    order = Order(user_id=user.id, status=status, total_amount=Decimal(total), currency="INR")
    if placed_at is not None:
        order.placed_at = placed_at
    for sku, qty, price in items or [("SKU-1", 2, "24.99")]:
        order.items.append(
            OrderItem(sku=sku, name=f"Item {sku}", quantity=qty, unit_price=Decimal(price))
        )
    session.add(order)
    await session.flush()
    return order


async def create_payment(
    session: AsyncSession, order: Order, *, amount: str | None = None
) -> Payment:
    payment = Payment(
        order_id=order.id,
        amount=Decimal(amount) if amount else order.total_amount,
        currency=order.currency,
        status=PaymentStatus.succeeded,
    )
    session.add(payment)
    await session.flush()
    return payment


async def create_refund(
    session: AsyncSession,
    payment: Payment,
    requested_by: User,
    *,
    idempotency_key: str,
    amount: str = "10.00",
) -> Refund:
    refund = Refund(
        idempotency_key=idempotency_key,
        payment_id=payment.id,
        order_id=payment.order_id,
        requested_by=requested_by.id,
        amount=Decimal(amount),
        currency=payment.currency,
    )
    session.add(refund)
    await session.flush()
    return refund
