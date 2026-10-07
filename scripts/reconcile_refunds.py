"""Retry refunds stuck in `processing` (a crash after the payout, or a provider timeout).

Usage (from the repo root, with the backend venv):
    backend/.venv/Scripts/python scripts/reconcile_refunds.py [options]

Options: --older-than SECONDS, --limit N, --dry-run (see --help).

Safe to run repeatedly and concurrently with live traffic: each refund is retried through the
same payout flow with the same provider key (`refund-<refund id>`), so the provider returns the
original result instead of paying again, and a refund that was meanwhile completed is left alone.
Nothing is held open (no transaction, no lock) while the provider is called.

SAFETY: with the SIMULATED provider the idempotency ledger lives in memory, so a separate process
starts with an empty ledger and would pay a stuck refund out a second time. The script therefore
REFUSES to run (exit code 2) against the simulated provider unless --allow-simulated is given.
--dry-run only lists and never contacts a provider, so it needs no flag. With a real provider
the ledger is the provider's. The tests prove exactly-once by sharing one provider instance.
"""

import argparse
import asyncio
import sys
from datetime import timedelta

from app.core.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.modules.refunds.payments import get_payment_provider
from app.modules.refunds.reconcile import find_stuck, reconcile_refusal, reconcile_stuck_refunds


async def main() -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--older-than",
        type=int,
        default=settings.refund_stuck_after_seconds,
        help="only refunds in 'processing' for longer than this many seconds "
        f"(default {settings.refund_stuck_after_seconds}, REFUND_STUCK_AFTER_SECONDS)",
    )
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true", help="list stuck refunds, change nothing")
    ap.add_argument(
        "--allow-simulated",
        action="store_true",
        help="DEVELOPMENT ONLY: allow running against the in-memory simulated provider "
        "(it would re-pay refunds it cannot remember)",
    )
    args = ap.parse_args()
    older_than = timedelta(seconds=args.older_than)

    provider = get_payment_provider()
    if not args.dry_run:
        refusal = reconcile_refusal(provider, allow_simulated=args.allow_simulated)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2  # before any database access

    maker = get_sessionmaker()
    try:
        if args.dry_run:
            async with maker() as session:
                ids = await find_stuck(session, older_than=older_than, limit=args.limit)
            print(f"{len(ids)} refund(s) stuck in 'processing' for > {args.older_than}s")
            for refund_id in ids:
                print(f"  {refund_id}")
            return 0

        results = await reconcile_stuck_refunds(
            maker, provider, older_than=older_than, limit=args.limit
        )
        print(f"reconciled {len(results)} refund(s) stuck for > {args.older_than}s")
        for r in results:
            print(f"  {r.refund_id}  {r.outcome}" + (f"  ({r.detail})" if r.detail else ""))
        return 1 if any(r.outcome == "error" for r in results) else 0
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
