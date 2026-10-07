"""Seed dev data: 1 admin, 2 customers, 10 orders (with items and payments).

Usage (from the repo root, with the backend venv):
    backend/.venv/Scripts/python scripts/seed.py

Idempotent: does nothing if the seed admin already exists. Refuses to run when APP_ENV=prod.
Passwords come from SEED_PASSWORD (default below) and are for local development only.
"""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Order, OrderItem, Payment, User
from app.db.models.enums import OrderStatus, PaymentStatus, Role
from app.db.session import get_engine, get_sessionmaker

DEFAULT_PASSWORD = "password123"

ADMIN = ("admin@example.com", "Ada Admin")
ALICE = ("alice@example.com", "Alice Customer")
BOB = ("bob@example.com", "Bob Customer")

CURRENCY = "INR"

# Prices in rupees. Phase 2 policy thresholds are in rupees too (auto-approve <= 500).
CATALOG: dict[str, tuple[str, Decimal]] = {
    "CB-005": ("HDMI Cable", Decimal("299.00")),
    "MS-002": ("Wireless Mouse", Decimal("799.00")),
    "ST-008": ("Laptop Stand", Decimal("1299.00")),
    "HB-003": ("USB-C Hub", Decimal("1499.00")),
    "WC-007": ("Webcam", Decimal("1999.00")),
    "KB-001": ("Mechanical Keyboard", Decimal("2499.00")),
    "HP-006": ("Headphones", Decimal("3499.00")),
    "MN-004": ("27in Monitor", Decimal("4999.00")),
}

S = OrderStatus
# (customer, status, [(sku, qty), ...], days_ago). Deliberately covers the cases the
# Phase 2 policy engine and the tests care about:
#   - delivered, total <= 500        (#1)
#   - delivered, total > 500         (#2, #7)
#   - placed 45 days ago             (#7)
#   - cancelled                      (#6, #10)
#   - pending / unpaid (no payment)  (#5)
ORDERS: list[tuple[tuple[str, str], OrderStatus, list[tuple[str, int]], int]] = [
    (ALICE, S.delivered, [("CB-005", 1)], 40),  # 1: 299
    (ALICE, S.delivered, [("KB-001", 1), ("MS-002", 1)], 25),  # 2: 3298
    (ALICE, S.shipped, [("HP-006", 1)], 6),  # 3: 3499
    (ALICE, S.paid, [("WC-007", 1), ("ST-008", 1)], 2),  # 4: 3298
    (ALICE, S.pending, [("HB-003", 1)], 0),  # 5: 1499, unpaid
    (ALICE, S.cancelled, [("CB-005", 2)], 12),  # 6: 598
    (BOB, S.delivered, [("MN-004", 1)], 45),  # 7: 4999, 45 days old
    (BOB, S.shipped, [("KB-001", 1), ("HB-003", 1)], 4),  # 8: 3998
    (BOB, S.paid, [("MS-002", 1), ("CB-005", 1)], 1),  # 9: 1098
    (BOB, S.cancelled, [("ST-008", 1)], 18),  # 10: 1299
]

PAID_STATUSES = {S.paid, S.shipped, S.delivered}


async def seed(session: AsyncSession, password: str = DEFAULT_PASSWORD) -> bool:
    """Insert seed data. Returns False (and changes nothing) if it was already seeded."""
    existing = await session.scalar(select(User.id).where(User.email == ADMIN[0]))
    if existing is not None:
        return False

    password_hash = hash_password(password)
    users: dict[str, User] = {}
    for (email, name), role in ((ADMIN, Role.admin), (ALICE, Role.customer), (BOB, Role.customer)):
        users[email] = User(email=email, full_name=name, role=role, password_hash=password_hash)
    session.add_all(users.values())
    await session.flush()

    now = datetime.now(UTC)
    for n, ((email, _), status, lines, days_ago) in enumerate(ORDERS, start=1):
        items = [
            OrderItem(sku=sku, name=CATALOG[sku][0], quantity=qty, unit_price=CATALOG[sku][1])
            for sku, qty in lines
        ]
        total = sum((i.unit_price * i.quantity for i in items), Decimal("0.00"))
        order = Order(
            user_id=users[email].id,
            status=status,
            total_amount=total,
            currency=CURRENCY,
            placed_at=now - timedelta(days=days_ago),
            items=items,
        )
        session.add(order)
        await session.flush()
        if status in PAID_STATUSES:
            session.add(
                Payment(
                    order_id=order.id,
                    amount=total,
                    currency=CURRENCY,
                    status=PaymentStatus.succeeded,
                    provider="simulated",
                    provider_ref=f"sim_{n:04d}",
                )
            )
    await session.flush()
    return True


async def main() -> int:
    settings = get_settings()
    if settings.is_prod:
        print("Refusing to seed: APP_ENV=prod", file=sys.stderr)
        return 1

    password = os.environ.get("SEED_PASSWORD", DEFAULT_PASSWORD)
    async with get_sessionmaker()() as session:
        created = await seed(session, password)
        await session.commit()
    await get_engine().dispose()

    if not created:
        print("Already seeded (admin@example.com exists); nothing to do.")
        return 0
    print("Seeded 3 users and 10 orders. Login with:")
    for email, _ in (ADMIN, ALICE, BOB):
        print(f"  {email} / {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
