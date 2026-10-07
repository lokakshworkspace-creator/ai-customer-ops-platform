"""Resolve refunds stuck in `processing` (the process died, or the provider never answered).

Safe by construction: a stuck refund is retried through the same `payout_flow` with the same
provider key (`refund-<id>`), so the provider returns the original result instead of paying out
again, and the finalise step does nothing if the refund was already completed.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Refund
from app.db.models.enums import RefundStatus
from app.modules.refunds.payments import PaymentProvider, SimulatedPaymentProvider
from app.modules.refunds.service import payout_flow

SIMULATED_REFUSAL = (
    "Refusing to reconcile with the SIMULATED payment provider.\n"
    "Its idempotency ledger lives in this process's memory, so a fresh process has an empty ledger "
    "and would pay a stuck refund out AGAIN (the original payout is unknown to it). Reconciling "
    "is only safe against a provider that remembers payouts by key, i.e. a real one.\n"
    "For local development only, pass --allow-simulated to accept that."
)


def reconcile_refusal(provider: PaymentProvider, *, allow_simulated: bool) -> str | None:
    """A reason NOT to run a reconcile with `provider`, or None if it is fine to proceed."""
    if isinstance(provider, SimulatedPaymentProvider) and not allow_simulated:
        return SIMULATED_REFUSAL
    return None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    refund_id: uuid.UUID
    outcome: str  # succeeded | failed | unconfirmed | already_final | error
    detail: str | None = None


async def find_stuck(
    session: AsyncSession, *, older_than: timedelta, limit: int = 50
) -> list[uuid.UUID]:
    """`processing` refunds not touched for `older_than` (database clock). No locks taken."""
    rows = await session.scalars(
        select(Refund.id)
        .where(
            Refund.status == RefundStatus.processing,
            Refund.updated_at < func.now() - older_than,
        )
        .order_by(Refund.updated_at, Refund.id)
        .limit(limit)
    )
    return list(rows)


async def reconcile_stuck_refunds(
    maker: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    *,
    older_than: timedelta,
    limit: int = 50,
    request_id: str | None = "reconcile",
) -> list[ReconcileResult]:
    async with maker() as session:
        ids = await find_stuck(session, older_than=older_than, limit=limit)
        await session.commit()

    results: list[ReconcileResult] = []
    for refund_id in ids:
        try:
            async with maker() as session:  # one session per refund: failures are isolated
                out = await payout_flow(session, provider, refund_id, request_id=request_id)
            results.append(ReconcileResult(refund_id, out.kind, out.error))
        except Exception as exc:
            results.append(ReconcileResult(refund_id, "error", f"{type(exc).__name__}: {exc}"))
    return results
