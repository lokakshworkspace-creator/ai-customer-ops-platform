"""Golden rule 7: outbox events are written in the SAME transaction as the data change. Also: the
routers copy the user into a plain Actor before the executor runs and never touch the ORM user
afterwards."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers import approvals as approvals_router
from app.api.routers import refunds as refunds_router
from app.db.models import Approval, OutboxEvent, Refund, ToolCall
from app.db.models.enums import ApprovalStatus, RefundStatus, Role, ToolCallStatus
from app.events.types import EventType
from app.modules.refunds import service as refund_service
from app.policy.rules import get_rules
from app.policy.types import Actor
from app.tests.worlds import World, make_world
from app.tools.context import ToolContext
from app.tools.default import build_default_registry
from app.tools.executor import ToolExecutor, ToolOutcome
from app.tools.handlers import refunds as refund_handlers


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await make_world(db_session)


def make_ctx(who, key: str = "atomic-key-0001") -> ToolContext:
    return ToolContext(
        actor=Actor(id=who.id, role=who.role), request_id="req-atomic-1", idempotency_key=key
    )


async def n(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


def args(world: World, amount: str = "100.00") -> dict:
    return {"order_id": str(world.order), "amount": amount, "reason_code": "damaged"}


# --- request: events commit or roll back WITH the refund ---


async def test_a_failure_after_the_events_were_added_rolls_back_the_refund_and_its_events(
    db_session, world, provider, monkeypatch
) -> None:
    def explode(*a, **kw):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(refund_handlers.audit, "record", explode)  # runs AFTER the refund + event
    ex = ToolExecutor(build_default_registry(get_rules(), provider))
    out = await ex.run(db_session, make_ctx(world.alice), "request_refund", args(world))

    assert (out.status, out.error_code, out.http_status) == (
        ToolCallStatus.failed,
        "INTERNAL_ERROR",
        500,
    )
    assert await n(db_session, Refund) == 0
    assert await n(db_session, OutboxEvent) == 0  # no event for a refund that does not exist
    assert await n(db_session, Approval) == 0 and provider.calls == 0
    assert await n(db_session, ToolCall) == 1  # but the attempt itself is on record


async def test_an_approval_request_failure_leaves_no_refund_no_approval_and_no_event(
    db_session, world, provider, monkeypatch
) -> None:
    real_emit = refund_service.emit

    def emit_then_fail(*a, **kw):
        real_emit(*a, **kw)  # the event row IS added to the session...
        raise RuntimeError("late failure")  # ...and then the request blows up

    monkeypatch.setattr(refund_service, "emit", emit_then_fail)
    ex = ToolExecutor(build_default_registry(get_rules(), provider))
    out = await ex.run(db_session, make_ctx(world.alice), "request_refund", args(world, "800.00"))
    assert out.status == ToolCallStatus.failed
    assert (
        await n(db_session, Refund),
        await n(db_session, OutboxEvent),
        await n(db_session, Approval),
    ) == (0, 0, 0)


async def test_the_events_are_committed_with_the_refund_even_if_the_process_dies_before_the_payout(
    db_session, world, provider, monkeypatch
) -> None:
    """Dying right after the transaction commits must still leave the refund AND its events."""

    async def die(*a, **kw):
        raise asyncio.CancelledError  # stands in for the process being killed

    monkeypatch.setattr(refund_handlers, "payout_flow", die)
    ex = ToolExecutor(build_default_registry(get_rules(), provider))
    with pytest.raises(asyncio.CancelledError):
        await ex.run(db_session, make_ctx(world.alice), "request_refund", args(world))
    await db_session.rollback()  # whatever was not committed is gone

    (refund,) = list(await db_session.scalars(select(Refund)))
    assert refund.status == RefundStatus.processing
    types = list(await db_session.scalars(select(OutboxEvent.event_type).order_by(OutboxEvent.seq)))
    assert types == ["refund.requested", "refund.approved"]
    (call,) = list(await db_session.scalars(select(ToolCall)))
    assert call.status == ToolCallStatus.pending  # visibly unfinished: the reconciler's job
    assert provider.calls == 0


# --- approve / reject ---


async def pending_refund(session: AsyncSession, world: World, provider) -> uuid.UUID:
    ex = ToolExecutor(build_default_registry(get_rules(), provider))
    out = await ex.run(session, make_ctx(world.alice), "request_refund", args(world, "800.00"))
    assert out.status == ToolCallStatus.awaiting_approval
    return uuid.UUID(out.data["approval_id"])


async def test_a_failure_while_approving_leaves_the_approval_pending_and_emits_nothing(
    db_session, world, provider, monkeypatch
) -> None:
    approval_id = await pending_refund(db_session, world, provider)

    def explode(*a, **kw):
        raise RuntimeError("audit failure")

    monkeypatch.setattr(refund_handlers.audit, "record", explode)
    ex = ToolExecutor(build_default_registry(get_rules(), provider))
    out = await ex.run(
        db_session, make_ctx(world.admin), "approve_refund", {"approval_id": str(approval_id)}
    )
    assert out.status == ToolCallStatus.failed

    approval = await db_session.scalar(select(Approval).execution_options(populate_existing=True))
    refund = await db_session.scalar(select(Refund).execution_options(populate_existing=True))
    assert (
        approval.status == ApprovalStatus.pending and refund.status == RefundStatus.pending_approval
    )
    assert list(await db_session.scalars(select(OutboxEvent.event_type))) == ["refund.requested"]
    assert provider.calls == 0


async def test_the_completed_event_is_recorded_atomically_with_the_status_change(
    db_session, world, provider, monkeypatch
) -> None:
    real_emit = refund_service.emit

    def emit_but_not_completed(session, refund, customer_id, event_type, **kw):
        if event_type == EventType.REFUND_COMPLETED:
            raise RuntimeError("cannot write the completed event")
        return real_emit(session, refund, customer_id, event_type, **kw)

    monkeypatch.setattr(refund_service, "emit", emit_but_not_completed)
    ex = ToolExecutor(build_default_registry(get_rules(), provider))
    out = await ex.run(db_session, make_ctx(world.alice), "request_refund", args(world))
    assert out.error_code == "POST_COMMIT_ERROR"

    refund = await db_session.scalar(select(Refund).execution_options(populate_existing=True))
    # either BOTH the status change and its event, or NEITHER: here neither
    assert refund.status == RefundStatus.processing and refund.processed_at is None
    assert "refund.completed" not in set(await db_session.scalars(select(OutboxEvent.event_type)))


# --- routers: plain Actor first, ORM user never touched afterwards ---


class PoisonUser:
    """Stands in for the ORM `User`. Reading `id`/`role` is fine until the executor has been
    called; any access after that raises, as an expired ORM object would after a rollback."""

    def __init__(self) -> None:
        self.executor_called = False
        self._id, self._role = uuid.uuid4(), Role.customer

    def _guard(self, name: str):
        if self.executor_called:
            raise AssertionError(f"router touched user.{name} after the executor ran")

    @property
    def id(self):
        self._guard("id")
        return self._id

    @property
    def role(self):
        self._guard("role")
        return self._role

    def __getattr__(self, name: str):
        self._guard(name)
        raise AttributeError(name)


class StubExecutor:
    def __init__(self, user: PoisonUser) -> None:
        self.user, self.seen = user, None

    async def run(self, session, ctx, tool_name, raw_args):
        self.user.executor_called = True
        self.seen = (ctx, tool_name, raw_args)
        return ToolOutcome(
            tool_call_id=uuid.uuid4(),
            tool_name=tool_name,
            status=ToolCallStatus.succeeded,
            http_status=200,
            data={"ok": True},
        )


async def test_create_refund_uses_a_plain_actor_and_never_touches_the_user_afterwards() -> None:
    user = PoisonUser()
    ex = StubExecutor(user)
    request = SimpleNamespace(state=SimpleNamespace(request_id="req-1"))
    body = refunds_router.RequestRefundInput(
        order_id=uuid.uuid4(), amount="10.00", reason_code="damaged"
    )
    await refunds_router.create_refund(request, body, user, None, ex, "router-key-0001")  # type: ignore[arg-type]
    ctx, tool, _ = ex.seen
    assert isinstance(ctx.actor, Actor) and ctx.actor.id == user._id and tool == "request_refund"
    assert ctx.idempotency_key == "router-key-0001" and ctx.request_id == "req-1"


@pytest.mark.parametrize("fn", [approvals_router.approve, approvals_router.reject])
async def test_approve_and_reject_do_the_same(fn) -> None:
    user = PoisonUser()
    ex = StubExecutor(user)
    request = SimpleNamespace(state=SimpleNamespace(request_id="req-2"))
    await fn(uuid.uuid4(), request, user, None, ex, None)  # type: ignore[arg-type]
    ctx, tool, _ = ex.seen
    assert isinstance(ctx.actor, Actor) and tool in {"approve_refund", "reject_refund"}


def test_the_get_routes_also_use_a_plain_actor() -> None:
    import inspect

    src = inspect.getsource(refunds_router.get_refund)
    assert "Actor(id=user.id, role=user.role)" in src
