"""Approval expiry: the sweeper expires stale approvals, the decision handlers refuse an expired
(or overdue) approval, and nothing can both decide and expire the same approval.

Real committed data (the `committed_db` fixture) because the locks, the outbox row and the race
only mean something across separate transactions."""

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Approval, AuditLog, OutboxEvent, Refund
from app.db.models.enums import ApprovalStatus, RefundStatus
from app.jobs import sweeper as sweeper_module
from app.jobs.sweeper import Sweeper
from app.modules.refunds import expiry, repo
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.policy.rules import get_rules
from app.tests.agent_helpers import all_rows
from app.tests.conftest import BACKEND_DIR, CommittedDB, _test_db_url
from app.tests.resume_helpers import Turn, backdate, decide, waiting_turn
from app.tests.worlds import World, make_world

pytestmark = pytest.mark.usefixtures("committed_db")


@pytest.fixture
async def world(committed_db: CommittedDB) -> World:
    async with committed_db.maker() as s:
        w = await make_world(s)
        await s.commit()
    return w


async def approval_and_refund(db: CommittedDB, turn: Turn) -> tuple[Approval, Refund]:
    async with db.maker() as s:
        (a,) = await all_rows(s, Approval, Approval.id == turn.approval_id)
        (r,) = await all_rows(s, Refund, Refund.id == turn.refund_id)
        return a, r


async def events_of(db: CommittedDB, event_type: str) -> list[OutboxEvent]:
    async with db.maker() as s:
        return await all_rows(s, OutboxEvent, OutboxEvent.event_type == event_type)


async def new_turn(db: CommittedDB, world: World, hours_old: float | None = None) -> Turn:
    # 501 is above the automatic limit (needs a human) and five of them fit under the 3000.00 order
    turn = await waiting_turn(db, world, SimulatedPaymentProvider(), "501", world.order2)
    if hours_old is not None:
        await backdate(db, turn.approval_id, hours_old)
    return turn


async def expire(db: CommittedDB, approval_id: uuid.UUID, **kw) -> bool:
    async with db.maker() as s:
        return await expiry.expire_one(s, approval_id, **kw)


# --- expiring --------------------------------------------------------------------------------


async def test_a_stale_approval_expires_with_its_refund_an_event_and_an_audit_row(
    committed_db, world
) -> None:
    turn = await new_turn(committed_db, world, hours_old=49)
    assert await expire(committed_db, turn.approval_id, request_id="req-sweep") is True

    approval, refund = await approval_and_refund(committed_db, turn)
    assert approval.status == ApprovalStatus.expired
    assert approval.decided_by is None  # nobody decided
    assert approval.decided_at is not None
    assert "48 hours" in approval.decision_reason
    assert approval.expires_at == approval.created_at + timedelta(hours=48)
    assert refund.status == RefundStatus.expired
    assert refund.processed_at is not None

    (event,) = await events_of(committed_db, "refund.expired")
    assert event.aggregate_id == str(refund.id)
    data = event.payload["data"]
    assert data["status"] == "expired"
    assert data["customer_id"] == str(world.alice.id)
    assert event.payload["meta"]["request_id"] == "req-sweep"
    assert not {"rejected_by", "approved_by"} & set(data)

    async with committed_db.maker() as s:
        (row,) = await all_rows(s, AuditLog, AuditLog.action == "refund.expired")
    assert row.actor_user_id is None  # the system
    assert row.resource_id == str(refund.id)


async def test_an_approval_inside_its_limit_is_left_alone(committed_db, world) -> None:
    turn = await new_turn(committed_db, world, hours_old=47)
    assert await expire(committed_db, turn.approval_id) is False
    approval, refund = await approval_and_refund(committed_db, turn)
    assert approval.status == ApprovalStatus.pending
    assert refund.status == RefundStatus.pending_approval
    assert await events_of(committed_db, "refund.expired") == []


async def test_expiring_twice_changes_nothing_the_second_time(committed_db, world) -> None:
    turn = await new_turn(committed_db, world, hours_old=60)
    assert await expire(committed_db, turn.approval_id) is True
    assert await expire(committed_db, turn.approval_id) is False
    assert len(await events_of(committed_db, "refund.expired")) == 1


@pytest.mark.parametrize("tool", ["approve_refund", "reject_refund"])
async def test_an_approval_that_was_decided_is_never_expired(
    committed_db, world, tool: str
) -> None:
    turn = await new_turn(committed_db, world)
    out = await decide(committed_db, turn, world, tool)
    assert out.http_status == 200
    await backdate(committed_db, turn.approval_id, 100)
    assert await expire(committed_db, turn.approval_id) is False
    approval, _ = await approval_and_refund(committed_db, turn)
    assert approval.status != ApprovalStatus.expired
    assert await events_of(committed_db, "refund.expired") == []


async def test_the_limit_is_a_setting(committed_db, world, monkeypatch: pytest.MonkeyPatch) -> None:
    turn = await new_turn(committed_db, world, hours_old=2)
    assert await expire(committed_db, turn.approval_id) is False  # 2 h < 48 h
    monkeypatch.setattr(get_settings(), "approval_ttl_hours", 1)
    assert await expire(committed_db, turn.approval_id) is True


async def test_expiring_releases_the_reserved_money_and_the_velocity_slot(
    committed_db, world
) -> None:
    rules = get_rules().refund
    turn = await new_turn(committed_db, world, hours_old=49)
    since = datetime.now(UTC) - timedelta(days=30)
    async with committed_db.maker() as s:
        assert (
            str(await repo.reserved_amount(s, world.order2, rules.reserving_statuses)) == "501.00"
        )
        assert (
            await repo.recent_count(s, world.alice.id, rules.velocity_counted_statuses, since=since)
            == 1
        )
    await expire(committed_db, turn.approval_id)
    async with committed_db.maker() as s:
        assert str(await repo.reserved_amount(s, world.order2, rules.reserving_statuses)) == "0"
        assert (
            await repo.recent_count(s, world.alice.id, rules.velocity_counted_statuses, since=since)
            == 0
        )


async def test_due_ids_are_only_pending_and_overdue_oldest_first_and_limited(
    committed_db, world
) -> None:
    now = datetime.now(UTC)
    t1 = await new_turn(committed_db, world, hours_old=50)
    t2 = await new_turn(committed_db, world, hours_old=70)
    t3 = await new_turn(committed_db, world, hours_old=1)  # not due
    t4 = await new_turn(committed_db, world)
    await decide(committed_db, t4, world, "reject_refund")  # decided (while still in time)
    await backdate(committed_db, t4.approval_id, 90)  # old, but decided: not due
    async with committed_db.maker() as s:
        ids = await expiry.due_approval_ids(s, now=now, limit=10)
        limited = await expiry.due_approval_ids(s, now=now, limit=1)
    assert ids == [t2.approval_id, t1.approval_id]  # oldest first
    assert t3.approval_id not in ids and t4.approval_id not in ids
    assert limited == [t2.approval_id]


# --- deciding an expired or overdue approval is refused --------------------------------------


@pytest.mark.parametrize("tool", ["approve_refund", "reject_refund"])
async def test_a_swept_approval_cannot_be_decided(committed_db, world, tool: str) -> None:
    turn = await new_turn(committed_db, world, hours_old=49)
    await expire(committed_db, turn.approval_id)
    out = await decide(committed_db, turn, world, tool)
    assert out.http_status == 409
    assert out.error_code == "APPROVAL_EXPIRED"
    approval, refund = await approval_and_refund(committed_db, turn)
    assert approval.status == ApprovalStatus.expired
    assert refund.status == RefundStatus.expired  # nothing was paid


@pytest.mark.parametrize("tool", ["approve_refund", "reject_refund"])
async def test_an_overdue_approval_the_sweeper_has_not_reached_yet_is_refused_too(
    committed_db, world, tool: str
) -> None:
    """The window between the deadline and the next sweep: still `pending`, but too late."""
    turn = await new_turn(committed_db, world, hours_old=49)
    out = await decide(committed_db, turn, world, tool)
    assert out.http_status == 409
    assert out.error_code == "APPROVAL_EXPIRED"
    approval, refund = await approval_and_refund(committed_db, turn)
    assert approval.status == ApprovalStatus.pending  # the refusal changed nothing
    assert refund.status == RefundStatus.pending_approval
    assert await expire(committed_db, turn.approval_id) is True  # the sweeper then expires it


async def test_the_guard_uses_the_deadline_not_just_the_status(
    committed_db, world, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = await new_turn(committed_db, world, hours_old=10)
    monkeypatch.setattr(get_settings(), "approval_ttl_hours", 12)
    assert (await decide(committed_db, turn, world)).http_status == 200  # 10 h < 12 h: allowed

    turn2 = await new_turn(committed_db, world, hours_old=10)
    monkeypatch.setattr(get_settings(), "approval_ttl_hours", 9)
    assert (await decide(committed_db, turn2, world)).error_code == "APPROVAL_EXPIRED"


async def test_an_approval_inside_its_limit_is_still_decidable(committed_db, world) -> None:
    turn = await new_turn(committed_db, world, hours_old=47)
    out = await decide(committed_db, turn, world)
    assert out.http_status == 200
    approval, _ = await approval_and_refund(committed_db, turn)
    assert approval.status == ApprovalStatus.approved


async def test_an_already_decided_approval_still_says_already_decided(committed_db, world) -> None:
    turn = await new_turn(committed_db, world)
    await decide(committed_db, turn, world, "reject_refund")
    out = await decide(committed_db, turn, world)
    assert out.error_code == "ALREADY_DECIDED"  # the old answer, not "expired"


# --- the race: a decision and the sweeper at the same moment ---------------------------------


async def test_a_decision_and_the_sweeper_on_an_overdue_approval_end_expired_exactly_once(
    committed_db, world
) -> None:
    """Both contenders reach the same approval at the same moment. It is overdue, so the handler
    refuses and the sweeper expires it: one expiry, one event, never an approved refund."""
    turns = [await new_turn(committed_db, world, hours_old=49) for _ in range(3)]
    results = await asyncio.gather(
        *[decide(committed_db, t, world) for t in turns],
        *[expire(committed_db, t.approval_id) for t in turns],
    )
    assert all(r.error_code == "APPROVAL_EXPIRED" for r in results[:3])
    for t in turns:
        approval, refund = await approval_and_refund(committed_db, t)
        assert approval.status == ApprovalStatus.expired
        assert refund.status == RefundStatus.expired
    assert len(await events_of(committed_db, "refund.expired")) == 3


async def test_two_sweepers_on_the_same_approval_expire_it_once(committed_db, world) -> None:
    turn = await new_turn(committed_db, world, hours_old=49)
    results = await asyncio.gather(*[expire(committed_db, turn.approval_id) for _ in range(4)])
    assert sorted(results) == [False, False, False, True]
    assert len(await events_of(committed_db, "refund.expired")) == 1


async def test_contending_decisions_and_sweeps_on_one_order_do_not_deadlock(
    committed_db, world
) -> None:
    """All of these approvals are on the same order and customer: the handlers and the sweeper
    take the same locks in the same order, so nothing waits for each other in a cycle. (A hang
    would hit the per-test timeout.)"""
    due = [await new_turn(committed_db, world, hours_old=49) for _ in range(2)]
    live = [await new_turn(committed_db, world) for _ in range(2)]
    results = await asyncio.wait_for(
        asyncio.gather(
            *[expire(committed_db, t.approval_id) for t in due],
            *[decide(committed_db, t, world) for t in live],
            *[decide(committed_db, t, world, "reject_refund") for t in due],
        ),
        timeout=60,
    )
    assert results[0] is True and results[1] is True
    assert all(r.http_status == 200 for r in results[2:4])  # the live ones were decided
    assert all(r.error_code == "APPROVAL_EXPIRED" for r in results[4:])


async def test_a_decision_made_just_inside_the_limit_beats_a_late_sweeper(
    committed_db, world
) -> None:
    turn = await new_turn(committed_db, world, hours_old=47)
    assert (await decide(committed_db, turn, world)).http_status == 200
    await backdate(committed_db, turn.approval_id, 49)  # even if it is "overdue" afterwards
    assert await expire(committed_db, turn.approval_id) is False
    approval, refund = await approval_and_refund(committed_db, turn)
    assert approval.status == ApprovalStatus.approved
    assert refund.status in (RefundStatus.processing, RefundStatus.succeeded)


# --- the sweeper -----------------------------------------------------------------------------


async def test_a_sweep_expires_everything_due_and_purges_old_revocations(
    committed_db, world
) -> None:
    from app.modules.users import tokens as users_tokens

    due = [await new_turn(committed_db, world, hours_old=h) for h in (50, 60, 72)]
    fresh = await new_turn(committed_db, world, hours_old=1)
    async with committed_db.maker() as s:
        now = datetime.now(UTC)
        await users_tokens.revoke(
            s, jti="old", user_id=world.alice.id, expires_at=now - timedelta(hours=1)
        )
        await users_tokens.revoke(
            s, jti="live", user_id=world.alice.id, expires_at=now + timedelta(hours=1)
        )
        await s.commit()

    stats = await Sweeper(committed_db.maker, batch_size=2).sweep()  # 3 due, batches of 2
    assert (stats.expired_approvals, stats.purged_tokens, stats.failed) == (3, 1, 0)
    for t in due:
        assert (await approval_and_refund(committed_db, t))[0].status == ApprovalStatus.expired
    assert (await approval_and_refund(committed_db, fresh))[0].status == ApprovalStatus.pending
    assert len(await events_of(committed_db, "refund.expired")) == 3

    again = await Sweeper(committed_db.maker).sweep()
    assert (again.expired_approvals, again.purged_tokens) == (0, 0)  # idempotent


async def test_one_failing_approval_does_not_stop_the_sweep(
    committed_db, world, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = await new_turn(committed_db, world, hours_old=80)
    good = await new_turn(committed_db, world, hours_old=60)
    real = expiry.expire_one

    async def flaky(session, approval_id, **kw):
        if approval_id == bad.approval_id:
            raise RuntimeError("boom")
        return await real(session, approval_id, **kw)

    monkeypatch.setattr(sweeper_module.expiry, "expire_one", flaky)
    stats = await Sweeper(committed_db.maker).sweep()
    assert stats.failed == 1 and stats.expired_approvals == 1
    assert (await approval_and_refund(committed_db, good))[0].status == ApprovalStatus.expired
    assert (await approval_and_refund(committed_db, bad))[0].status == ApprovalStatus.pending


async def test_a_sweep_does_nothing_when_told_to_stop(committed_db, world) -> None:
    turn = await new_turn(committed_db, world, hours_old=60)
    stop = asyncio.Event()
    stop.set()
    stats = await Sweeper(committed_db.maker).sweep(stop)
    assert stats.expired_approvals == 0
    assert (await approval_and_refund(committed_db, turn))[0].status == ApprovalStatus.pending


async def test_the_sweeper_runs_as_a_real_process_with_once(committed_db, world) -> None:
    turn = await new_turn(committed_db, world, hours_old=60)
    env = dict(os.environ)
    env.update(
        DATABASE_URL=_test_db_url().render_as_string(hide_password=False),
        PYTHONIOENCODING="utf-8",
        APP_ENV="dev",
    )
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "app.jobs.sweeper", "--once"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
    assert (await approval_and_refund(committed_db, turn))[0].status == ApprovalStatus.expired


async def test_once_has_a_deadline_and_exits_3_naming_what_it_waited_on(
    committed_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang(self, stop=None):
        self.phase.set("expiring approvals (Postgres)")
        await asyncio.sleep(30)

    monkeypatch.setattr(Sweeper, "sweep", hang)
    code = await sweeper_module.main(["--once", "--deadline", "0.3"])
    assert code == 3


async def test_the_outbox_row_of_an_expiry_is_written_in_the_same_transaction(
    committed_db, world
) -> None:
    """If the transaction rolls back, there is no event and the approval is still pending."""
    turn = await new_turn(committed_db, world, hours_old=60)
    async with committed_db.maker() as s:
        # run expire_one's work but fail before the commit: the commit is the last statement
        real_commit = s.commit

        async def boom() -> None:
            await s.rollback()
            raise RuntimeError("commit failed")

        s.commit = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await expiry.expire_one(s, turn.approval_id)
        s.commit = real_commit  # type: ignore[method-assign]
    approval, refund = await approval_and_refund(committed_db, turn)
    assert approval.status == ApprovalStatus.pending
    assert refund.status == RefundStatus.pending_approval
    assert await events_of(committed_db, "refund.expired") == []
    async with committed_db.maker() as s:
        assert (
            await s.scalars(select(OutboxEvent).where(OutboxEvent.event_type == "refund.expired"))
        ).all() == []
