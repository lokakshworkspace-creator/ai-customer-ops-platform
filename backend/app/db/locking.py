"""Row locks, and THE lock order. This is the only place that defines it.

    LOCK ORDER: order  ->  user  ->  refund  ->  approval

Every transaction that takes more than one of these locks takes them in this order, never the
reverse, so two transactions can wait on each other but can never wait in a cycle (deadlock).

* A refund REQUEST locks the order, then the customer's user row, then INSERTS the refund and
  approval (new rows need no lock).
* An approve / reject locks the order, then the user, then the refund, then the approval. To
  know which order to lock it first reads the approval and refund WITHOUT locking, then
  re-reads them under the locks and re-checks their state.
* A payout FINALISE (after the provider call) and the reconcile job lock only the refund,
  a suffix of the order, so they fit.

Why each lock exists:
* order:  serialises refunds / cancels on one order, so "remaining refundable" cannot be
          over-committed by two concurrent requests.
* user:   serialises the per-customer velocity count (refunds across their different orders).

All locks are `FOR NO KEY UPDATE`, not `FOR UPDATE`: they conflict with each other but NOT with
the `FOR KEY SHARE` locks that foreign-key checks take, so unrelated inserts that merely
reference the user or order (tool_calls, tickets, ...) are not blocked.

Locks are held until the transaction ends. NEVER call an external service (the payment provider)
while holding one: that is why payouts run after the commit.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, Order, Refund, User

LOCK_ORDER = ("order", "user", "refund", "approval")


async def lock_order(
    session: AsyncSession, order_id: uuid.UUID, *, owner_id: uuid.UUID | None = None
) -> Order | None:
    """Lock and return the order. `owner_id` scopes it to one customer (None = admin: any)."""
    stmt = select(Order).where(Order.id == order_id)
    if owner_id is not None:
        stmt = stmt.where(Order.user_id == owner_id)
    stmt = stmt.with_for_update(key_share=True, of=Order).execution_options(populate_existing=True)
    return await session.scalar(stmt)


async def lock_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        select(User.id).where(User.id == user_id).with_for_update(key_share=True, of=User)
    )


async def lock_refund(session: AsyncSession, refund_id: uuid.UUID) -> Refund | None:
    stmt = (
        select(Refund)
        .where(Refund.id == refund_id)
        .with_for_update(key_share=True, of=Refund)
        .execution_options(populate_existing=True)  # current state, not a stale cached copy
    )
    return await session.scalar(stmt)


async def lock_approval(session: AsyncSession, approval_id: uuid.UUID) -> Approval | None:
    stmt = (
        select(Approval)
        .where(Approval.id == approval_id)
        .with_for_update(key_share=True, of=Approval)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)
