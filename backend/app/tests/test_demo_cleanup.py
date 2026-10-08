"""scripts/demo_cleanup.py against the real test database (ops_test, committed data)."""

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.db.models import (
    Approval,
    AuditLog,
    Message,
    Order,
    OrderItem,
    Payment,
    ProcessedEvent,
    Refund,
    Ticket,
    ToolCall,
    User,
    WorkflowRun,
)
from app.db.models.enums import (
    ApprovalStatus,
    MessageRole,
    OrderStatus,
    PaymentStatus,
    PolicyDecision,
    RefundStatus,
    Role,
    ToolCallStatus,
)
from app.events.outbox import add_event
from app.events.types import AggregateType, EventType
from app.tests.conftest import CommittedDB, _test_db_url
from app.tests.event_helpers import refund_data
from app.tests.test_dev_only_scripts import load

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "demo_cleanup.py"
BACKEND = REPO / "backend"

TABLES = [
    "users",
    "orders",
    "order_items",
    "payments",
    "refunds",
    "approvals",
    "tool_calls",
    "audit_logs",
    "outbox_events",
    "processed_events",
    "tickets",
    "workflow_runs",
    "messages",
]
# Everything is deleted by this script except nothing: every table above can hold demo rows.
PER_GROUP = {  # rows `world()` creates for ONE customer + ONE admin
    "users": 2,
    "orders": 2,
    "order_items": 2,
    "payments": 2,
    "refunds": 2,
    "approvals": 1,
    "tool_calls": 2,
    "audit_logs": 3,
    "outbox_events": 4,
    "processed_events": 4,
    "tickets": 1,
    "workflow_runs": 1,
    "messages": 2,
}


@dataclass
class Group:
    customer: uuid.UUID
    admin: uuid.UUID
    refunds: list[uuid.UUID]
    approval: uuid.UUID


async def world(maker: async_sessionmaker[AsyncSession], prefix: str, tag: str) -> Group:
    """One customer + one admin (emails `<prefix>-customer-<tag>@example.com` ...) with a full set
    of related rows: 2 delivered orders, 2 succeeded payments, 2 refunds (one with an approval
    decided by the admin), 2 tool calls, 3 audit rows, 4 outbox events with their dedupe rows, a
    ticket and a workflow run."""
    async with maker() as s:
        pw = hash_password("x-password-123")
        customer = User(
            email=f"{prefix}-customer-{tag}@example.com",
            full_name="C",
            role=Role.customer,
            password_hash=pw,
        )
        admin = User(
            email=f"{prefix}-admin-{tag}@example.com",
            full_name="A",
            role=Role.admin,
            password_hash=pw,
        )
        s.add_all([customer, admin])
        await s.flush()
        refunds: list[Refund] = []
        for n in (1, 2):
            order = Order(
                user_id=customer.id,
                status=OrderStatus.delivered,
                total_amount=Decimal("100.00"),
                currency="INR",
                placed_at=datetime.now(UTC),
                items=[OrderItem(sku=f"S{n}", name="item", quantity=1, unit_price=Decimal("100"))],
            )
            s.add(order)
            await s.flush()
            payment = Payment(
                order_id=order.id,
                amount=Decimal("100.00"),
                status=PaymentStatus.succeeded,
                provider="simulated",
                provider_ref=f"{prefix}_{tag}_{n}",
            )
            s.add(payment)
            await s.flush()
            refund = Refund(
                idempotency_key=f"{prefix}-{tag}-{n}",
                request_hash="0" * 64,
                payment_id=payment.id,
                order_id=order.id,
                requested_by=customer.id,
                amount=Decimal("10.00"),
                currency="INR",
                reason_code="damaged",
                status=RefundStatus.pending_approval if n == 1 else RefundStatus.succeeded,
            )
            s.add(refund)
            await s.flush()
            refunds.append(refund)
        calls = [
            ToolCall(
                user_id=customer.id,
                tool_name="request_refund",
                arguments={},
                status=ToolCallStatus.succeeded,
                policy_decision=PolicyDecision.ALLOW,
            ),
            ToolCall(
                user_id=admin.id,
                tool_name="approve_refund",
                arguments={},
                status=ToolCallStatus.succeeded,
            ),
        ]
        s.add_all(calls)
        await s.flush()
        approval = Approval(
            refund_id=refunds[0].id,
            tool_call_id=calls[0].id,
            requested_by=customer.id,
            decided_by=admin.id,
            action="refund",
            status=ApprovalStatus.approved,
        )
        s.add(approval)
        s.add_all(
            [
                # acted by the user, about a refund, and about a tool call
                AuditLog(
                    actor_user_id=admin.id,
                    action="refund.approve",
                    resource_type="refund",
                    resource_id=str(refunds[0].id),
                ),
                AuditLog(
                    actor_user_id=None,
                    action="refund.system",
                    resource_type="refund",
                    resource_id=str(refunds[1].id),
                ),  # nobody acted; still about a demo row
                AuditLog(actor_user_id=customer.id, action="login"),
            ]
        )
        s.add(Ticket(user_id=customer.id, subject="s", body="d"))
        run = WorkflowRun(user_id=customer.id, state="start")
        s.add(run)
        await s.flush()
        for role, who in ((MessageRole.user, customer.id), (MessageRole.assistant, customer.id)):
            s.add(
                Message(
                    conversation_id=uuid.uuid4(),
                    user_id=who,
                    workflow_run_id=run.id,
                    role=role,
                    content=[{"type": "text", "text": "hi"}],
                )
            )
        events = []
        for refund in refunds:
            for event_type in (EventType.REFUND_REQUESTED, EventType.REFUND_COMPLETED):
                data = refund_data(refund.id)
                if event_type is EventType.REFUND_COMPLETED:
                    data["provider_ref"] = "sim"
                events.append(
                    add_event(
                        s,
                        aggregate_type=AggregateType.REFUND,
                        aggregate_id=refund.id,
                        event_type=event_type,
                        data=data,
                    )
                )
        await s.flush()
        s.add_all(ProcessedEvent(consumer="notifications", event_id=e.id) for e in events)
        group = Group(customer.id, admin.id, [r.id for r in refunds], approval.id)
        await s.commit()
        return group


async def counts(maker: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with maker() as s:
        return {t: await s.scalar(text(f"SELECT count(*) FROM {t}")) for t in TABLES}


async def ids(maker: async_sessionmaker[AsyncSession]) -> dict[str, set]:
    async with maker() as s:
        out: dict[str, set] = {}
        for t in TABLES:
            key = "(consumer, event_id)" if t == "processed_events" else "id"
            rows = await s.execute(text(f"SELECT {key} FROM {t}"))
            out[t] = {tuple(r) if t == "processed_events" else r[0] for r in rows}
        return out


@pytest.fixture
def cleanup():
    return load("demo_cleanup")


async def standard_world(maker):
    """Two demo groups, plus rows that must never be touched: seeded-style users and look-alikes."""
    await world(maker, "demo", "run1")
    await world(maker, "demo", "run2")
    await world(maker, "alice", "x")  # 'alice-customer-x@...': an ordinary customer
    await world(maker, "notdemo", "x")  # contains 'demo' but does not START with 'demo-'
    await world(maker, "demo2", "x")  # starts with 'demo' but not 'demo-'
    await world(maker, "xdemo", "x")
    async with maker() as s:  # 'demo@...' and 'demo_x@...' have no hyphen after demo
        for email in ("demo@example.com", "demo_x@example.com", "admin@example.com"):
            s.add(User(email=email, full_name="L", role=Role.customer, password_hash="x"))
        await s.commit()


EXPECTED_DEMO = {t: 2 * n for t, n in PER_GROUP.items()}  # two demo groups


@pytest.mark.usefixtures("committed_db")
async def test_dry_run_counts_exactly_and_changes_nothing(committed_db: CommittedDB, cleanup):
    await standard_world(committed_db.maker)
    before = await ids(committed_db.maker)

    report = await cleanup.run(committed_db.maker, apply=False)

    assert report.applied is False
    assert report.counts == EXPECTED_DEMO
    assert report.emails == sorted(
        [f"demo-admin-run{n}@example.com" for n in (1, 2)]
        + [f"demo-customer-run{n}@example.com" for n in (1, 2)]
    )
    assert await ids(committed_db.maker) == before  # not one row changed


@pytest.mark.usefixtures("committed_db")
async def test_apply_deletes_exactly_what_the_dry_run_counted_and_nothing_else(
    committed_db: CommittedDB, cleanup
):
    await standard_world(committed_db.maker)
    before = await ids(committed_db.maker)
    dry = await cleanup.run(committed_db.maker, apply=False)

    done = await cleanup.run(committed_db.maker, apply=True)

    assert done.applied is True
    assert done.counts == dry.counts == EXPECTED_DEMO
    after = await ids(committed_db.maker)
    for table in TABLES:
        assert len(before[table]) - len(after[table]) == EXPECTED_DEMO[table], table
        assert after[table] <= before[table], table  # only deletions, nothing new
    async with committed_db.maker() as s:
        left = (await s.execute(text("SELECT email FROM users ORDER BY email"))).scalars().all()
    assert left == sorted(
        [
            "admin@example.com",
            "alice-admin-x@example.com",
            "alice-customer-x@example.com",
            "demo2-admin-x@example.com",
            "demo2-customer-x@example.com",
            "demo@example.com",
            "demo_x@example.com",
            "notdemo-admin-x@example.com",
            "notdemo-customer-x@example.com",
            "xdemo-admin-x@example.com",
            "xdemo-customer-x@example.com",
        ]
    )
    # the four look-alike / ordinary groups kept every one of their rows
    rest = await counts(committed_db.maker)
    assert rest == {t: 4 * n for t, n in PER_GROUP.items()} | {"users": 4 * 2 + 3}

    again = await cleanup.run(committed_db.maker, apply=True)  # nothing left: a no-op
    assert again.total == 0 and again.emails == []


@pytest.mark.usefixtures("committed_db")
async def test_with_no_demo_users_every_count_is_zero(committed_db: CommittedDB, cleanup):
    await world(committed_db.maker, "alice", "x")
    for apply in (False, True):
        report = await cleanup.run(committed_db.maker, apply=apply)
        assert report.total == 0 and set(report.counts) == set(TABLES)
    assert await counts(committed_db.maker) == PER_GROUP


@pytest.mark.usefixtures("committed_db")
async def test_other_users_rows_that_point_at_demo_rows_go_with_them(
    committed_db: CommittedDB, cleanup
):
    """A real admin's audit row about a demo refund, a real admin's refund of a demo order and an
    outbox event about it are all about demo data. Foreign keys force them to be deleted with it."""
    demo = await world(committed_db.maker, "demo", "run1")
    real = await world(committed_db.maker, "alice", "x")
    async with committed_db.maker() as s:
        s.add(
            AuditLog(
                actor_user_id=real.admin,
                action="refund.view",
                resource_type="refund",
                resource_id=str(demo.refunds[0]),
            )
        )
        # a REAL admin requested a refund against a DEMO order/payment, and it has an event
        order_id, payment_id = (
            await s.execute(
                text(
                    "SELECT order_id, id FROM payments "
                    "WHERE id = (SELECT payment_id FROM refunds WHERE id = :r)"
                ),
                {"r": demo.refunds[0]},
            )
        ).one()
        by_real_admin = Refund(
            idempotency_key="real-admin-refund",
            request_hash="1" * 64,
            payment_id=payment_id,
            order_id=order_id,
            requested_by=real.admin,
            amount=Decimal("5.00"),
            currency="INR",
            reason_code="damaged",
            status=RefundStatus.succeeded,
        )
        s.add(by_real_admin)
        await s.flush()
        add_event(
            s,
            aggregate_type=AggregateType.REFUND,
            aggregate_id=by_real_admin.id,
            event_type=EventType.REFUND_REQUESTED,
            data=refund_data(by_real_admin.id),
        )
        await s.commit()

    done = await cleanup.run(committed_db.maker, apply=True)

    assert done.counts["audit_logs"] == PER_GROUP["audit_logs"] + 1
    assert done.counts["refunds"] == PER_GROUP["refunds"] + 1  # the real admin's, on a demo order
    assert done.counts["outbox_events"] == PER_GROUP["outbox_events"] + 1
    async with committed_db.maker() as s:
        assert (
            await s.scalar(text("SELECT count(*) FROM audit_logs WHERE action='refund.view'")) == 0
        )
    rest = await counts(committed_db.maker)
    assert rest == PER_GROUP  # the 'alice' group is intact


# --- it refuses to delete anything when demo rows are entangled with real data ------------------


@pytest.mark.usefixtures("committed_db")
@pytest.mark.parametrize("apply", [False, True])
async def test_a_demo_admin_who_decided_a_real_approval_blocks_everything(
    committed_db: CommittedDB, cleanup, apply: bool
):
    demo = await world(committed_db.maker, "demo", "run1")
    real = await world(committed_db.maker, "alice", "x")
    async with committed_db.maker() as s:
        await s.execute(
            text("UPDATE approvals SET decided_by = :d WHERE id = :a"),
            {"d": demo.admin, "a": real.approval},
        )
        await s.commit()
    before = await ids(committed_db.maker)

    with pytest.raises(cleanup.Blocked, match="approval"):
        await cleanup.run(committed_db.maker, apply=apply)

    assert await ids(committed_db.maker) == before


@pytest.mark.usefixtures("committed_db")
@pytest.mark.parametrize("apply", [False, True])
async def test_a_demo_admin_refund_of_a_real_order_blocks_everything(
    committed_db: CommittedDB, cleanup, apply: bool
):
    demo = await world(committed_db.maker, "demo", "run1")
    real = await world(committed_db.maker, "alice", "x")
    async with committed_db.maker() as s:
        await s.execute(
            text("UPDATE refunds SET requested_by = :d WHERE id = :r"),
            {"d": demo.admin, "r": real.refunds[0]},
        )
        await s.commit()
    before = await ids(committed_db.maker)

    with pytest.raises(cleanup.Blocked, match="refund"):
        await cleanup.run(committed_db.maker, apply=apply)

    assert await ids(committed_db.maker) == before


@pytest.mark.usefixtures("committed_db")
async def test_apply_is_all_or_nothing(committed_db: CommittedDB, cleanup, monkeypatch):
    """If the last delete fails, the earlier ones are rolled back too."""
    await standard_world(committed_db.maker)
    before = await ids(committed_db.maker)
    real_statements = cleanup.statements

    def failing(plan):
        stmts = real_statements(plan)
        label, table, _ = stmts[-1]
        assert label == "users"
        return [*stmts[:-1], (label, table, text("1/0 = 1"))]  # blows up at the very end

    monkeypatch.setattr(cleanup, "statements", failing)
    with pytest.raises(Exception, match="division by zero"):
        await cleanup.run(committed_db.maker, apply=True)

    assert await ids(committed_db.maker) == before


@pytest.mark.usefixtures("committed_db")
async def test_the_deletes_respect_foreign_key_order(committed_db: CommittedDB, cleanup):
    """Every table with demo data is emptied without a foreign-key error, including a demo
    user who owns nothing else but a ticket or a workflow run."""
    await world(committed_db.maker, "demo", "run1")
    async with committed_db.maker() as s:
        lone = User(
            email="demo-lone@example.com", full_name="L", role=Role.customer, password_hash="x"
        )
        s.add(lone)
        await s.flush()
        s.add(WorkflowRun(user_id=lone.id, state="start"))
        await s.commit()
    done = await cleanup.run(committed_db.maker, apply=True)
    assert done.counts["users"] == 3 and done.counts["workflow_runs"] == 2
    assert all(v == 0 for v in (await counts(committed_db.maker)).values())


# --- it refuses outside dev, before touching anything -------------------------------------------


@pytest.mark.parametrize("env", ["test", "prod", "staging"])
@pytest.mark.parametrize("apply", [False, True])
async def test_it_refuses_outside_dev_before_touching_anything(
    monkeypatch, capsys, cleanup, env: str, apply: bool
):
    def touched(*a, **kw):
        raise AssertionError("touched the database before refusing")

    monkeypatch.setattr(cleanup, "run", touched)
    import app.db.session as session

    monkeypatch.setattr(session, "get_engine", touched)
    monkeypatch.setenv("APP_ENV", env)

    assert await cleanup.main(apply) == 2
    err = capsys.readouterr().err
    assert f"demo_cleanup.py refuses to run with APP_ENV={env!r}" in err
    assert "Nothing was touched" in err


# --- the command line, as a real process, against ops_test --------------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "APP_ENV": "dev",
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": _test_db_url().render_as_string(hide_password=False),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,  # a cold start costs ~11 s of imports
    )


@pytest.mark.usefixtures("committed_db")
async def test_the_command_line_is_a_dry_run_unless_told_otherwise(committed_db: CommittedDB):
    await standard_world(committed_db.maker)
    before = await counts(committed_db.maker)

    dry = run_cli()
    assert dry.returncode == 0, dry.stderr[-800:]
    assert "DRY RUN" in dry.stdout and "would delete" in dry.stdout
    assert "database:" in dry.stdout and "_test" in dry.stdout and "***" in dry.stdout
    assert "demo-customer-run1@example.com" in dry.stdout
    for table, n in EXPECTED_DEMO.items():
        assert any(line.split() == [table, str(n)] for line in dry.stdout.splitlines()), (
            table,
            dry.stdout,
        )
    assert await counts(committed_db.maker) == before

    applied = run_cli("--apply")
    assert applied.returncode == 0, applied.stderr[-800:]
    assert "DRY RUN" not in applied.stdout and "deleted" in applied.stdout
    after = await counts(committed_db.maker)
    assert {t: before[t] - after[t] for t in TABLES} == EXPECTED_DEMO


def test_the_command_line_exits_2_outside_dev_without_a_traceback():
    env = {
        **os.environ,
        "APP_ENV": "prod",
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": "postgresql+asyncpg://x:x@127.0.0.1:1/x",
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--apply"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    assert "demo_cleanup.py refuses to run with APP_ENV='prod'" in done.stderr
    assert "Traceback" not in done.stderr and done.stdout == ""
