"""List outbox events and un-park the ones the relay gave up on.

Usage (from the repo root, with the backend venv):
    backend/.venv/Scripts/python scripts/outbox_admin.py list [--state STATE]
    backend/.venv/Scripts/python scripts/outbox_admin.py blocked
    backend/.venv/Scripts/python scripts/outbox_admin.py unpark SELECTOR [--dry-run]

STATE is pending|parked|published|all. SELECTOR is exactly one of: --id UUID (repeatable),
--aggregate AGGREGATE_ID, --all.

A PARKED event has exhausted its retries. It is still unpublished, so it blocks every later event
of the same aggregate (ordering is preserved on purpose). Fix the cause (broker, bad data), then
un-park: attempts are reset and the relay picks the event up on its next poll.
"""

import argparse
import asyncio
import sys
import uuid

from app.db.session import get_engine, get_sessionmaker
from app.events import admin


def show(rows: list[dict]) -> None:
    if not rows:
        print("(none)")
    for r in rows:
        print("  ".join(f"{k}={v}" for k, v in r.items()))


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="list outbox events")
    p_list.add_argument(
        "--state", choices=["pending", "parked", "published", "all"], default="parked"
    )
    p_list.add_argument("--limit", type=int, default=50)
    sub.add_parser("blocked", help="parked events and how many later events they block")
    p_un = sub.add_parser("unpark", help="make parked events eligible again")
    p_un.add_argument(
        "--id", dest="ids", action="append", type=uuid.UUID, help="event id (repeatable)"
    )
    p_un.add_argument("--aggregate", help="every parked event of this aggregate id")
    p_un.add_argument("--all", dest="all_parked", action="store_true", help="every parked event")
    p_un.add_argument(
        "--dry-run", action="store_true", help="show what would change, change nothing"
    )
    args = ap.parse_args(argv)

    maker = get_sessionmaker()
    try:
        async with maker() as session:
            if args.cmd == "list":
                show(await admin.list_events(session, args.state, args.limit))
                return 0
            if args.cmd == "blocked":
                show(await admin.blocked_by_parked(session))
                return 0

            chosen = [bool(args.ids), bool(args.aggregate), args.all_parked]
            if sum(chosen) != 1:
                print("unpark: give exactly one of --id, --aggregate, --all", file=sys.stderr)
                return 2
            if args.dry_run:
                print("would un-park:")
                show(
                    [
                        r
                        for r in await admin.list_events(session, "parked", 10_000)
                        if (
                            args.all_parked
                            or (args.ids and r["id"] in args.ids)
                            or r["aggregate_id"] == args.aggregate
                        )
                    ]
                )
                return 0
            n = await admin.unpark(
                session, event_ids=args.ids, aggregate_id=args.aggregate, all_parked=args.all_parked
            )
            await session.commit()
            print(f"un-parked {n} event(s); the relay will retry them on its next poll")
            return 0
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
