"""The sweeper: periodic housekeeping that no request triggers. Run it as its own process:

    python -m app.jobs.sweeper            # forever, until SIGINT / SIGTERM
    python -m app.jobs.sweeper --once     # one full sweep, then exit

THE SWEEPER MUST BE RUNNING. Without it, approvals never expire (a refund nobody decides waits
forever, and so does its customer) and logged-out tokens are never purged. A decision made after
the deadline is refused either way (see app/modules/refunds/expiry.py), so a stopped sweeper makes
the system stale, not unsafe.

One sweep does two things:
1. EXPIRE approvals that are still pending `APPROVAL_TTL_HOURS` after they were created: the
   approval and its refund become `expired` and a `refund.expired` event goes to the outbox in the
   same transaction. One approval per transaction, so a failure affects one approval only. A batch
   of `--batch-size` is processed and the sweep repeats until nothing is due.
2. PURGE `revoked_tokens` rows whose token has expired on its own (it is invalid anyway).

It is safe to run twice: every expiry re-checks under the same row locks the approve and reject
handlers use, so the second runner finds nothing left to do.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine, get_sessionmaker
from app.events.runtime import (
    DeadlineExceeded,
    Phase,
    install_signal_handlers,
    run_with_deadline,
    sleep_or_stop,
)
from app.modules.refunds import expiry
from app.modules.users import tokens as users_tokens

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class SweepStats:
    expired_approvals: int = 0
    purged_tokens: int = 0
    failed: int = 0


class Sweeper:
    def __init__(self, maker: async_sessionmaker, *, batch_size: int = 50) -> None:
        self._maker = maker
        self._batch_size = batch_size
        self.phase = Phase()

    async def sweep(self, stop: asyncio.Event | None = None) -> SweepStats:
        """One full sweep: expire everything due, then purge revoked tokens."""
        stats = SweepStats()
        stop = stop or asyncio.Event()
        self.phase.set("expiring approvals (Postgres)")
        skipped: set = set()
        while not stop.is_set():
            async with self._maker() as session:
                ids = [
                    i
                    for i in await expiry.due_approval_ids(
                        session, now=datetime.now(UTC), limit=self._batch_size + len(skipped)
                    )
                    if i not in skipped
                ]
                await session.rollback()
            if not ids:
                break
            for approval_id in ids[: self._batch_size]:
                if stop.is_set():
                    break
                try:
                    async with self._maker() as session:
                        if await expiry.expire_one(session, approval_id):
                            stats.expired_approvals += 1
                        else:
                            skipped.add(approval_id)  # decided meanwhile, or not due after all
                except Exception:
                    # One bad approval must not stop the sweep; it is retried on the next one.
                    stats.failed += 1
                    skipped.add(approval_id)
                    log.exception("approval_expiry_failed", approval_id=str(approval_id))
        self.phase.set("purging expired revoked tokens (Postgres)")
        async with self._maker() as session:
            stats.purged_tokens = await users_tokens.purge_expired(session)
            await session.commit()
        if stats.expired_approvals or stats.purged_tokens or stats.failed:
            log.info(
                "sweep_finished",
                expired_approvals=stats.expired_approvals,
                purged_tokens=stats.purged_tokens,
                failed=stats.failed,
            )
        return stats


async def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="Expire stale approvals and purge old revocations.")
    ap.add_argument("--once", action="store_true", help="run one full sweep, then exit")
    ap.add_argument("--batch-size", type=int, default=settings.sweeper_batch_size)
    ap.add_argument("--interval", type=float, default=settings.sweeper_interval_s)
    ap.add_argument(
        "--deadline",
        type=float,
        default=settings.once_deadline_s,
        help="with --once: give up after this many seconds and exit 3, naming what it was "
        f"waiting on (default {settings.once_deadline_s:g}, ONCE_DEADLINE_S; 0 = no limit)",
    )
    args = ap.parse_args(argv)
    configure_logging(settings.log_level, json_logs=settings.is_prod)

    sweeper = Sweeper(get_sessionmaker(), batch_size=args.batch_size)
    stop = asyncio.Event()
    restore = install_signal_handlers(stop)
    try:
        if args.once:
            log.info("sweeper_started", once=True, ttl_hours=settings.approval_ttl_hours)
            try:
                await run_with_deadline(
                    sweeper.sweep(stop), args.deadline, sweeper.phase, "sweeper --once"
                )
            except DeadlineExceeded as exc:
                log.error(
                    "sweeper_deadline_exceeded", waiting_on=exc.waiting_on, seconds=exc.seconds
                )
                print(f"sweeper: {exc}", file=sys.stderr)
                return 3
        else:
            log.info(
                "sweeper_started",
                once=False,
                ttl_hours=settings.approval_ttl_hours,
                interval_s=args.interval,
            )
            while not stop.is_set():
                try:
                    await sweeper.sweep(stop)
                except Exception:
                    log.exception("sweep_failed", phase=sweeper.phase.current)
                if await sleep_or_stop(stop, args.interval):
                    break
        log.info("sweeper_stopped")
        return 0
    finally:
        restore()
        await get_engine().dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
