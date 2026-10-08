"""Remove the throw-away rows that scripts/demo_phase2.py leaves behind in the DEV database.

    backend/.venv/Scripts/python scripts/demo_cleanup.py            # dry run: counts only (default)
    backend/.venv/Scripts/python scripts/demo_cleanup.py --apply    # actually delete

WHAT COUNTS AS DEMO DATA. Users whose email starts with "demo-" and everything that belongs to
them: their chat messages and workflow runs, their orders (and order items), the payments of those
orders, refunds (requested by them or against their orders/payments), approvals (of those refunds,
or requested/decided by them), tool_calls made by them, audit_logs (acted by them, or about any of
the rows above), outbox events about those rows, the consumer-dedupe rows (processed_events) of
those outbox events, and tickets. Nothing else is selected; the seeded users and every other user
are never touched.

SAFETY.
  * Dry run by default; it prints the exact counts that --apply would delete, and --apply deletes
    exactly those rows (both use the same predicates), in ONE transaction: all or nothing.
  * Refuses to run unless APP_ENV is "dev" (exit 2, before any database access).
  * If a demo row is entangled with data that is NOT demo data (for example a demo admin decided
    somebody else's approval), it deletes nothing, explains why, and exits 1.
  * Only deletes rows. It never drops, truncates or resets anything.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, Table, delete, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import dev_only_refusal
from app.db.models import (
    Approval,
    AuditLog,
    Message,
    Order,
    OrderItem,
    OutboxEvent,
    Payment,
    ProcessedEvent,
    Refund,
    Ticket,
    ToolCall,
    User,
    WorkflowRun,
)

DEMO_PREFIX = "demo-"
SHOW_EMAILS = 10


class Blocked(Exception):
    """Demo rows are entangled with non-demo data; nothing may be deleted."""


@dataclass
class Plan:
    """Ids of every row that belongs to the demo users. Built read-only."""

    users: list[tuple[Any, str]] = field(default_factory=list)  # (id, email)
    user_ids: list[Any] = field(default_factory=list)
    order_ids: list[Any] = field(default_factory=list)
    payment_ids: list[Any] = field(default_factory=list)
    refund_ids: list[Any] = field(default_factory=list)
    approval_ids: list[Any] = field(default_factory=list)
    tool_call_ids: list[Any] = field(default_factory=list)
    ticket_ids: list[Any] = field(default_factory=list)
    workflow_ids: list[Any] = field(default_factory=list)
    message_ids: list[Any] = field(default_factory=list)
    audit_ids: list[Any] = field(default_factory=list)
    outbox_ids: list[Any] = field(default_factory=list)

    @property
    def resource_ids(self) -> list[str]:
        """Every demo entity id as text (audit_logs.resource_id / outbox aggregate_id are text)."""
        groups = (
            self.order_ids,
            self.payment_ids,
            self.refund_ids,
            self.approval_ids,
            self.tool_call_ids,
            self.ticket_ids,
            self.workflow_ids,
            self.user_ids,
        )
        return [str(i) for g in groups for i in g]


def _in(column: Any, ids: list[Any]) -> ColumnElement[bool]:
    return column.in_(ids) if ids else false()


async def _ids(s: AsyncSession, column: Any, where: ColumnElement[bool]) -> list[Any]:
    return list((await s.scalars(select(column).where(where))).all())


async def build_plan(s: AsyncSession) -> Plan:
    p = Plan()
    rows = (
        await s.execute(
            select(User.id, User.email)
            .where(func.left(User.email, len(DEMO_PREFIX)) == DEMO_PREFIX)
            .order_by(User.email)
        )
    ).all()
    p.users = [(r.id, r.email) for r in rows]
    p.user_ids = [r.id for r in rows]
    if not p.user_ids:
        return p

    p.order_ids = await _ids(s, Order.id, _in(Order.user_id, p.user_ids))
    p.payment_ids = await _ids(s, Payment.id, _in(Payment.order_id, p.order_ids))
    p.refund_ids = await _ids(
        s,
        Refund.id,
        or_(
            _in(Refund.requested_by, p.user_ids),
            _in(Refund.order_id, p.order_ids),
            _in(Refund.payment_id, p.payment_ids),
        ),
    )
    p.approval_ids = await _ids(
        s,
        Approval.id,
        or_(
            _in(Approval.refund_id, p.refund_ids),
            _in(Approval.requested_by, p.user_ids),
            _in(Approval.decided_by, p.user_ids),
        ),
    )
    p.tool_call_ids = await _ids(s, ToolCall.id, _in(ToolCall.user_id, p.user_ids))
    p.ticket_ids = await _ids(s, Ticket.id, _in(Ticket.user_id, p.user_ids))
    p.workflow_ids = await _ids(s, WorkflowRun.id, _in(WorkflowRun.user_id, p.user_ids))
    p.message_ids = await _ids(s, Message.id, _in(Message.user_id, p.user_ids))
    ids = p.resource_ids
    p.audit_ids = await _ids(
        s, AuditLog.id, or_(_in(AuditLog.actor_user_id, p.user_ids), _in(AuditLog.resource_id, ids))
    )
    p.outbox_ids = await _ids(s, OutboxEvent.id, _in(OutboxEvent.aggregate_id, ids))
    await check_not_entangled(s, p)
    return p


async def check_not_entangled(s: AsyncSession, p: Plan) -> None:
    """Refuse when deleting would take non-demo data with it."""
    problems: list[str] = []

    # A refund a demo user requested against somebody else's order.
    stray_refunds = await _ids(
        s,
        Refund.id,
        _in(Refund.requested_by, p.user_ids) & ~_in(Refund.order_id, p.order_ids),
    )
    if stray_refunds:
        problems.append(
            f"{len(stray_refunds)} refund(s) requested by a demo user are against orders of "
            f"non-demo users (e.g. {stray_refunds[0]})"
        )
    # An approval that touches a demo user but belongs to a refund that is not being deleted.
    stray_approvals = await _ids(
        s,
        Approval.id,
        or_(_in(Approval.requested_by, p.user_ids), _in(Approval.decided_by, p.user_ids))
        & (Approval.refund_id.is_(None) | ~_in(Approval.refund_id, p.refund_ids)),
    )
    if stray_approvals:
        problems.append(
            f"{len(stray_approvals)} approval(s) requested or decided by a demo user are not about "
            f"a demo refund (e.g. {stray_approvals[0]})"
        )
    if problems:
        raise Blocked("; ".join(problems))


def statements(p: Plan) -> list[tuple[str, Table, ColumnElement[bool]]]:
    """(label, table, predicate) in an order that satisfies every foreign key."""
    return [
        ("processed_events", ProcessedEvent.__table__, _in(ProcessedEvent.event_id, p.outbox_ids)),
        ("outbox_events", OutboxEvent.__table__, _in(OutboxEvent.id, p.outbox_ids)),
        ("audit_logs", AuditLog.__table__, _in(AuditLog.id, p.audit_ids)),
        ("approvals", Approval.__table__, _in(Approval.id, p.approval_ids)),
        ("tool_calls", ToolCall.__table__, _in(ToolCall.id, p.tool_call_ids)),
        ("refunds", Refund.__table__, _in(Refund.id, p.refund_ids)),
        ("payments", Payment.__table__, _in(Payment.id, p.payment_ids)),
        ("order_items", OrderItem.__table__, _in(OrderItem.order_id, p.order_ids)),
        ("tickets", Ticket.__table__, _in(Ticket.id, p.ticket_ids)),
        ("orders", Order.__table__, _in(Order.id, p.order_ids)),
        ("messages", Message.__table__, _in(Message.id, p.message_ids)),
        ("workflow_runs", WorkflowRun.__table__, _in(WorkflowRun.id, p.workflow_ids)),
        ("users", User.__table__, _in(User.id, p.user_ids)),
    ]


@dataclass
class Report:
    applied: bool
    emails: list[str]
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


async def run(maker: async_sessionmaker[AsyncSession], *, apply: bool) -> Report:
    """Count (dry run) or delete (apply) the demo rows. Raises Blocked, deleting nothing."""
    async with maker() as s:
        async with s.begin():
            plan = await build_plan(s)
            counts: dict[str, int] = {}
            for label, table, where in statements(plan):
                if apply:
                    counts[label] = (await s.execute(delete(table).where(where))).rowcount
                else:
                    counts[label] = (
                        await s.scalar(select(func.count()).select_from(table).where(where)) or 0
                    )
        # `s.begin()` committed on exit; a dry run changed nothing to commit.
    return Report(apply, [e for _, e in plan.users], counts)


def render(report: Report, database: str) -> str:
    verb = "deleted" if report.applied else "would delete"
    lines = [
        f"database: {database}",
        f"demo users (email starts with {DEMO_PREFIX!r}): {len(report.emails)}",
    ]
    lines += [f"  {e}" for e in report.emails[:SHOW_EMAILS]]
    if len(report.emails) > SHOW_EMAILS:
        lines.append(f"  ... and {len(report.emails) - SHOW_EMAILS} more")
    lines.append(f"rows {verb}:")
    width = max(len(k) for k in report.counts)
    lines += [f"  {k:<{width}}  {v:>6}" for k, v in report.counts.items()]
    lines.append(f"  {'TOTAL':<{width}}  {report.total:>6}")
    lines.append(
        "applied: all of the above was deleted in one transaction."
        if report.applied
        else "DRY RUN: nothing was changed. Re-run with --apply to delete exactly these rows."
    )
    return "\n".join(lines)


async def main(apply: bool) -> int:
    refusal = dev_only_refusal("demo_cleanup.py")  # before ANY settings or database access
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    from app.db.session import get_engine, get_sessionmaker

    engine = get_engine()
    try:
        try:
            report = await run(get_sessionmaker(), apply=apply)
        except Blocked as exc:
            print(f"BLOCKED, nothing was deleted: {exc}", file=sys.stderr)
            return 1
        print(render(report, engine.url.render_as_string(hide_password=True)))
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="delete (default is a dry run)")
    sys.exit(asyncio.run(main(parser.parse_args().apply)))
