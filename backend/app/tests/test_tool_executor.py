"""The executor pipeline: validation -> authorization -> policy -> execution -> logging.

Real tools are used where they reach the path; configurable fake tools cover the paths the
real tools do not yet reach (approval, post-commit work, failures, huge payloads).
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, OutboxEvent, Ticket, ToolCall, User
from app.db.models.enums import (
    OrderStatus,
    PolicyDecision,
    Role,
    ToolCallStatus,
)
from app.db.models.tool_call import MAX_ARGUMENTS_BYTES, MAX_RESULT_BYTES
from app.modules.tickets import service as tickets_service
from app.policy import PolicyResult, ReasonCode
from app.policy.types import Actor
from app.tests.factories import create_order, create_user
from app.tests.policy_helpers import make_rules
from app.tools.context import ToolContext
from app.tools.default import build_default_registry
from app.tools.executor import ToolExecutor
from app.tools.handlers.base import (
    AfterCommitResult,
    Decision,
    ToolError,
    ToolResult,
)
from app.tools.registry import ToolSpec
from app.tools.schemas import ToolInput

RULES = make_rules()


# --- fake tool ---------------------------------------------------------------------------


class FakeInput(ToolInput):
    note: str = "n"


class FakeHandler:
    """Configurable handler that records what the executor did with it."""

    def __init__(
        self,
        *,
        policy: PolicyResult | None = None,
        decide_raises: BaseException | None = None,
        execute_raises: BaseException | None = None,
        write_ticket_for: uuid.UUID | None = None,
        after_commit: Callable[[AsyncSession], Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.policy = policy or PolicyResult(
            PolicyDecision.ALLOW, ReasonCode.READ_ALLOWED, "ok", (ReasonCode.READ_ALLOWED,)
        )
        self.decide_raises = decide_raises
        self.execute_raises = execute_raises
        self.write_ticket_for = write_ticket_for
        self.after_commit = after_commit
        self.data = data if data is not None else {"ok": True}
        self.decided = 0
        self.executed = 0

    async def decide(self, session, ctx, args, rules) -> Decision:
        self.decided += 1
        if self.decide_raises:
            raise self.decide_raises
        return Decision(self.policy)

    async def execute(self, session, ctx, args, decision) -> ToolResult:
        self.executed += 1
        if self.write_ticket_for:
            await tickets_service.create_ticket(
                session, user_id=self.write_ticket_for, subject="from fake", body="b"
            )
        if self.execute_raises:
            raise self.execute_raises
        return ToolResult(data=dict(self.data), after_commit=self.after_commit)


def deny(code=ReasonCode.ORDER_TOO_OLD) -> PolicyResult:
    return PolicyResult(PolicyDecision.DENY, code, "denied by test policy", (code,))


def ask() -> PolicyResult:
    return PolicyResult(
        PolicyDecision.REQUIRE_APPROVAL,
        ReasonCode.REFUND_ABOVE_AUTO_LIMIT,
        "needs a human",
        (ReasonCode.REFUND_ABOVE_AUTO_LIMIT,),
    )


def make_executor(**fakes: tuple[FakeHandler, frozenset[Role]]) -> ToolExecutor:
    reg = build_default_registry(RULES)
    for name, (handler, roles) in fakes.items():
        reg.register(
            ToolSpec(
                name=name,
                description="fake",
                input_model=FakeInput,
                required_roles=roles,
                mutating=True,
                agent_visible=False,
                handler=handler,
            )
        )
    return ToolExecutor(reg, rules_provider=lambda: RULES)


BOTH = frozenset({Role.customer, Role.admin})
ADMIN = frozenset({Role.admin})


def ctx_for(user: User, **kw: Any) -> ToolContext:
    return ToolContext(
        actor=Actor(id=user.id, role=user.role),
        request_id=kw.pop("request_id", "req-12345678"),
        **kw,
    )


async def row(session: AsyncSession, call_id: uuid.UUID) -> ToolCall:
    return (
        await session.execute(
            select(ToolCall).where(ToolCall.id == call_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def audit(session: AsyncSession, call_id: uuid.UUID) -> list[AuditLog]:
    return list(
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.resource_id == str(call_id))
            .execution_options(populate_existing=True)
        )
    )


async def count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
async def alice(db_session: AsyncSession) -> User:
    return await create_user(db_session, email="alice@example.com")


@pytest.fixture
async def bob(db_session: AsyncSession) -> User:
    return await create_user(db_session, email="bob@example.com")


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    return await create_user(db_session, email="admin@example.com", role=Role.admin)


# --- success / read-only ------------------------------------------------------------------


async def test_get_order_succeeds_and_is_fully_logged(db_session, alice) -> None:
    order = await create_order(db_session, alice)
    ex = make_executor()
    out = await ex.run(db_session, ctx_for(alice), "get_order", {"order_id": str(order.id)})

    assert out.status == ToolCallStatus.succeeded and out.http_status == 200
    assert out.decision == PolicyDecision.ALLOW and out.reason_code == "READ_ALLOWED"
    assert out.data["order"]["id"] == str(order.id)
    assert out.data["order"]["total_amount"] == "49.98"  # decimal string, not a float

    call = await row(db_session, out.tool_call_id)
    assert (call.tool_name, call.status, call.user_id) == (
        "get_order",
        ToolCallStatus.succeeded,
        alice.id,
    )
    assert call.arguments == {"order_id": str(order.id)}
    assert call.policy_decision == PolicyDecision.ALLOW
    assert call.policy_reason_code == "READ_ALLOWED" and call.policy_reason
    assert call.result["order"]["id"] == str(order.id)
    assert call.request_id == "req-12345678" and call.finished_at is not None
    assert call.error_code is None

    (a,) = await audit(db_session, out.tool_call_id)
    assert (a.action, a.actor_user_id, a.decision, a.reason_code) == (
        "tool.get_order",
        alice.id,
        PolicyDecision.ALLOW,
        "READ_ALLOWED",
    )
    assert a.request_id == "req-12345678"


async def test_list_orders_is_owner_scoped(db_session, alice, bob, admin) -> None:
    await create_order(db_session, alice)
    await create_order(db_session, alice)
    await create_order(db_session, bob)
    ex = make_executor()
    mine = await ex.run(db_session, ctx_for(alice), "list_orders", {})
    assert mine.data["total"] == 2 and len(mine.data["orders"]) == 2
    everyone = await ex.run(db_session, ctx_for(admin), "list_orders", {"limit": 100})
    assert everyone.data["total"] == 3


async def test_admin_can_read_any_order(db_session, alice, admin) -> None:
    order = await create_order(db_session, alice)
    out = await make_executor().run(
        db_session, ctx_for(admin), "get_order", {"order_id": str(order.id)}
    )
    assert out.status == ToolCallStatus.succeeded


async def test_another_customers_order_is_not_found_and_logged_as_failed(
    db_session, alice, bob
) -> None:
    bobs_id = (await create_order(db_session, bob)).id  # ids first: rollbacks expire ORM objects
    ex, ctx = (
        make_executor(),
        ctx_for(alice),
    )  # build the context once: rollbacks expire ORM objects
    theirs = await ex.run(db_session, ctx, "get_order", {"order_id": str(bobs_id)})
    nothing = await ex.run(db_session, ctx, "get_order", {"order_id": str(uuid.uuid4())})

    for out in (theirs, nothing):
        assert (out.status, out.http_status, out.error_code) == (
            ToolCallStatus.failed,
            404,
            "NOT_FOUND",
        )
        assert out.data is None
    assert theirs.error == nothing.error  # indistinguishable
    call = await row(db_session, theirs.tool_call_id)
    assert call.status == ToolCallStatus.failed and call.error_code == "NOT_FOUND"
    assert str(bobs_id) in str(call.arguments)  # the attempt itself is on record


# --- validation ---------------------------------------------------------------------------


async def test_unknown_tool_fails_safely_and_is_logged(db_session, alice) -> None:
    out = await make_executor().run(db_session, ctx_for(alice), "drop_database", {"x": 1})
    assert (out.status, out.error_code, out.http_status) == (
        ToolCallStatus.invalid,
        "UNKNOWN_TOOL",
        404,
    )
    call = await row(db_session, out.tool_call_id)
    assert call.tool_name == "drop_database" and call.status == ToolCallStatus.invalid
    assert call.arguments == {"x": 1} and call.error_code == "UNKNOWN_TOOL"
    assert len(await audit(db_session, out.tool_call_id)) == 1


@pytest.mark.parametrize(
    "args",
    [
        {},  # missing required field
        {"order_id": "not-a-uuid"},
        {"order_id": 12345},
        {"order_id": None},
        {"order_id": str(uuid.uuid4()), "extra": "field"},
        None,  # treated as {}: still missing order_id
        ["a", "list"],
        "a string",
        42,
    ],
)
async def test_invalid_arguments_are_rejected_and_logged(db_session, alice, args) -> None:
    out = await make_executor().run(db_session, ctx_for(alice), "get_order", args)
    assert (out.status, out.error_code, out.http_status) == (
        ToolCallStatus.invalid,
        "INVALID_ARGUMENTS",
        422,
    )
    assert out.details  # field-level problems are returned
    assert all("input" not in d for d in out.details)  # attacker-controlled values are not echoed
    call = await row(db_session, out.tool_call_id)
    assert call.status == ToolCallStatus.invalid and call.error_code == "INVALID_ARGUMENTS"
    assert call.policy_decision is None  # the policy never ran


async def test_a_user_id_argument_is_rejected_not_ignored(db_session, alice, bob) -> None:
    ex = make_executor()
    out = await ex.run(
        db_session,
        ctx_for(alice),
        "create_ticket",
        {"subject": "help", "body": "please", "user_id": str(bob.id)},
    )
    assert (out.status, out.error_code) == (ToolCallStatus.invalid, "INVALID_ARGUMENTS")
    assert any(d["field"] == "user_id" for d in out.details)
    assert await count(db_session, Ticket) == 0  # nothing was created for anyone
    call = await row(db_session, out.tool_call_id)
    assert call.arguments["user_id"] == str(bob.id)  # the attempt is on record


@pytest.mark.parametrize("field", ["user_id", "actor_id", "requested_by", "role", "customer_id"])
async def test_no_identity_argument_is_ever_accepted(db_session, alice, bob, field) -> None:
    order = await create_order(db_session, bob)
    out = await make_executor().run(
        db_session, ctx_for(alice), "get_order", {"order_id": str(order.id), field: str(bob.id)}
    )
    assert out.status == ToolCallStatus.invalid


async def test_identity_always_comes_from_the_context(db_session, alice, bob) -> None:
    out = await make_executor().run(
        db_session, ctx_for(alice), "create_ticket", {"subject": "help", "body": "please"}
    )
    assert out.status == ToolCallStatus.succeeded and out.http_status == 201
    ticket = await db_session.scalar(select(Ticket))
    assert ticket.user_id == alice.id
    assert (await row(db_session, out.tool_call_id)).user_id == alice.id


async def test_ticket_cannot_reference_another_customers_order(db_session, alice, bob) -> None:
    bobs = await create_order(db_session, bob)
    out = await make_executor().run(
        db_session,
        ctx_for(alice),
        "create_ticket",
        {"subject": "s", "body": "b", "order_id": str(bobs.id)},
    )
    assert (out.status, out.error_code) == (ToolCallStatus.failed, "NOT_FOUND")
    assert await count(db_session, Ticket) == 0


# --- authorization ------------------------------------------------------------------------


async def test_authorization_failure_is_denied_logged_and_never_reaches_the_handler(
    db_session, alice
) -> None:
    handler = FakeHandler()
    ex = make_executor(admin_ping=(handler, ADMIN))
    out = await ex.run(db_session, ctx_for(alice), "admin_ping", {})

    assert (out.status, out.http_status) == (ToolCallStatus.denied, 403)
    assert (out.decision, out.reason_code) == (PolicyDecision.DENY, "ROLE_NOT_PERMITTED")
    assert handler.decided == 0 and handler.executed == 0
    call = await row(db_session, out.tool_call_id)
    assert call.status == ToolCallStatus.denied
    assert (
        call.policy_decision == PolicyDecision.DENY
        and call.policy_reason_code == "ROLE_NOT_PERMITTED"
    )
    (a,) = await audit(db_session, out.tool_call_id)
    assert (a.decision, a.reason_code) == (PolicyDecision.DENY, "ROLE_NOT_PERMITTED")


async def test_the_right_role_passes_authorization(db_session, admin) -> None:
    handler = FakeHandler()
    out = await make_executor(admin_ping=(handler, ADMIN)).run(
        db_session, ctx_for(admin), "admin_ping", {}
    )
    assert out.status == ToolCallStatus.succeeded and handler.executed == 1


# --- policy -------------------------------------------------------------------------------


async def test_policy_deny_blocks_execution_and_is_logged(db_session, alice) -> None:
    order = await create_order(db_session, alice, status=OrderStatus.paid)
    out = await make_executor().run(
        db_session, ctx_for(alice), "cancel_order", {"order_id": str(order.id)}
    )

    assert (out.status, out.http_status) == (ToolCallStatus.denied, 403)
    assert (out.decision, out.reason_code) == (PolicyDecision.DENY, "CANCEL_REQUIRES_REFUND")
    assert "refund" in out.reason.lower()
    await db_session.refresh(order)
    assert order.status == OrderStatus.paid  # untouched

    call = await row(db_session, out.tool_call_id)
    assert (
        call.policy_decision == PolicyDecision.DENY
        and call.policy_reason_code == "CANCEL_REQUIRES_REFUND"
    )
    assert call.policy_reason and call.status == ToolCallStatus.denied
    (a,) = await audit(db_session, out.tool_call_id)
    assert (a.decision, a.reason_code, a.action) == (
        PolicyDecision.DENY,
        "CANCEL_REQUIRES_REFUND",
        "tool.cancel_order",
    )
    assert a.details["all_reason_codes"] == ["CANCEL_REQUIRES_REFUND"]


async def test_policy_deny_never_calls_execute(db_session, alice) -> None:
    handler = FakeHandler(policy=deny(), write_ticket_for=alice.id)
    out = await make_executor(fake=(handler, BOTH)).run(db_session, ctx_for(alice), "fake", {})
    assert out.status == ToolCallStatus.denied
    assert (handler.decided, handler.executed) == (1, 0)
    assert await count(db_session, Ticket) == 0


async def test_policy_allow_executes_and_persists_the_effect(db_session, alice) -> None:
    order = await create_order(db_session, alice, status=OrderStatus.pending)
    out = await make_executor().run(
        db_session, ctx_for(alice), "cancel_order", {"order_id": str(order.id)}
    )
    assert out.status == ToolCallStatus.succeeded and out.reason_code == "CANCEL_ALLOWED"
    await db_session.refresh(order)
    assert order.status == OrderStatus.cancelled
    assert (await row(db_session, out.tool_call_id)).policy_decision == PolicyDecision.ALLOW


async def test_require_approval_still_executes_and_is_marked_awaiting(db_session, alice) -> None:
    handler = FakeHandler(policy=ask(), write_ticket_for=alice.id)
    out = await make_executor(fake=(handler, BOTH)).run(db_session, ctx_for(alice), "fake", {})
    assert (out.status, out.decision) == (
        ToolCallStatus.awaiting_approval,
        PolicyDecision.REQUIRE_APPROVAL,
    )
    assert handler.executed == 1  # the handler is what parks the work for a human
    call = await row(db_session, out.tool_call_id)
    assert call.status == ToolCallStatus.awaiting_approval
    assert call.policy_reason_code == "REFUND_ABOVE_AUTO_LIMIT"
    assert await count(db_session, Ticket) == 1


# --- failures ------------------------------------------------------------------------------


async def test_tool_error_rolls_back_and_reports_the_safe_code(db_session, alice) -> None:
    handler = FakeHandler(decide_raises=ToolError("CONFLICT", "already done", http_status=409))
    out = await make_executor(fake=(handler, BOTH)).run(db_session, ctx_for(alice), "fake", {})
    assert (out.status, out.error_code, out.http_status, out.error) == (
        ToolCallStatus.failed,
        "CONFLICT",
        409,
        "already done",
    )
    assert (await row(db_session, out.tool_call_id)).error_code == "CONFLICT"


async def test_unexpected_exception_rolls_back_writes_hides_details_and_is_logged(
    db_session, alice
) -> None:
    handler = FakeHandler(
        write_ticket_for=alice.id, execute_raises=RuntimeError("secret db detail")
    )
    out = await make_executor(fake=(handler, BOTH)).run(db_session, ctx_for(alice), "fake", {})

    assert (out.status, out.error_code, out.http_status) == (
        ToolCallStatus.failed,
        "INTERNAL_ERROR",
        500,
    )
    assert out.error == "internal error" and "secret" not in str(out)
    assert await count(db_session, Ticket) == 0  # the write was rolled back...
    assert await count(db_session, OutboxEvent) == 0  # ...and so was its outbox event
    call = await row(db_session, out.tool_call_id)
    assert (
        call.status == ToolCallStatus.failed and "secret db detail" in call.error
    )  # internal log keeps it
    assert len(await audit(db_session, out.tool_call_id)) == 1


async def test_a_crash_mid_pipeline_still_leaves_a_pending_trace(db_session, alice) -> None:
    """The start row is committed before anything else runs."""
    handler = FakeHandler(decide_raises=asyncio.CancelledError())
    ex = make_executor(fake=(handler, BOTH))
    with pytest.raises(asyncio.CancelledError):
        await ex.run(db_session, ctx_for(alice), "fake", {"note": "x"})
    await db_session.rollback()
    (call,) = list(await db_session.scalars(select(ToolCall).where(ToolCall.tool_name == "fake")))
    assert call.status == ToolCallStatus.pending and call.arguments == {"note": "x"}


async def test_run_refuses_a_session_with_uncommitted_changes(db_session, alice) -> None:
    db_session.add(Ticket(user_id=alice.id, subject="s", body="b"))  # pending, unflushed
    with pytest.raises(RuntimeError, match="pending changes"):
        await make_executor().run(db_session, ctx_for(alice), "list_orders", {})


# --- after-commit step ---------------------------------------------------------------------


async def test_after_commit_runs_after_the_transaction_and_sets_the_final_status(
    db_session, alice
) -> None:
    seen: dict[str, Any] = {}

    async def hook(session: AsyncSession) -> AfterCommitResult:
        # The effect is already committed, and the call is still in flight.
        seen["tickets"] = await count(session, Ticket)
        seen["status"] = await session.scalar(
            select(ToolCall.status).where(ToolCall.tool_name == "fake")
        )
        return AfterCommitResult(status=ToolCallStatus.succeeded, data={"payout": "done"})

    handler = FakeHandler(write_ticket_for=alice.id, after_commit=hook)
    out = await make_executor(fake=(handler, BOTH)).run(db_session, ctx_for(alice), "fake", {})

    assert seen == {"tickets": 1, "status": ToolCallStatus.pending}
    assert out.status == ToolCallStatus.succeeded and out.data == {"ok": True, "payout": "done"}
    call = await row(db_session, out.tool_call_id)
    assert call.status == ToolCallStatus.succeeded and call.result == {"ok": True, "payout": "done"}
    assert call.finished_at is not None


async def test_after_commit_reporting_failure_is_recorded(db_session, alice) -> None:
    async def hook(session: AsyncSession) -> AfterCommitResult:
        return AfterCommitResult(
            status=ToolCallStatus.failed, error_code="PAYOUT_FAILED", error="declined"
        )

    out = await make_executor(fake=(FakeHandler(after_commit=hook), BOTH)).run(
        db_session, ctx_for(alice), "fake", {}
    )
    assert (out.status, out.error_code) == (ToolCallStatus.failed, "PAYOUT_FAILED")
    assert (await row(db_session, out.tool_call_id)).error == "declined"


async def test_after_commit_crash_keeps_the_committed_effect_and_marks_the_call_failed(
    db_session, alice
) -> None:
    async def hook(session: AsyncSession) -> AfterCommitResult:
        raise ConnectionError("provider down")

    handler = FakeHandler(write_ticket_for=alice.id, after_commit=hook)
    out = await make_executor(fake=(handler, BOTH)).run(db_session, ctx_for(alice), "fake", {})
    assert (out.status, out.error_code) == (ToolCallStatus.failed, "POST_COMMIT_ERROR")
    assert await count(db_session, Ticket) == 1  # the committed effect is NOT undone


# --- storage caps --------------------------------------------------------------------------


async def test_huge_arguments_are_truncated_in_the_log_but_the_call_still_proceeds(
    db_session, alice
) -> None:
    huge = "A" * 300_000
    out = await make_executor().run(
        db_session, ctx_for(alice), "get_order", {"order_id": huge, "junk": huge}
    )
    assert out.status == ToolCallStatus.invalid  # rejected on its merits
    call = await row(db_session, out.tool_call_id)
    assert call.arguments["_truncated"] is True and call.arguments["_original_bytes"] > 300_000
    assert len(str(call.arguments)) < MAX_ARGUMENTS_BYTES
    assert huge not in call.error  # the error text isn't a copy of the payload either


async def test_huge_results_are_truncated_in_the_log_but_returned_in_full(
    db_session, alice
) -> None:
    big = {"blob": "B" * (MAX_RESULT_BYTES * 2)}
    out = await make_executor(fake=(FakeHandler(data=big), BOTH)).run(
        db_session, ctx_for(alice), "fake", {}
    )
    assert out.status == ToolCallStatus.succeeded and out.data == big  # the caller gets everything
    call = await row(db_session, out.tool_call_id)
    assert call.result["_truncated"] is True and call.result["_original_bytes"] > MAX_RESULT_BYTES


async def test_non_json_native_arguments_are_stored_safely(db_session, alice) -> None:
    from decimal import Decimal

    out = await make_executor().run(
        db_session,
        ctx_for(alice),
        "get_order",
        {"order_id": uuid.uuid4(), "weird": Decimal("1.50")},
    )
    assert out.status == ToolCallStatus.invalid
    assert (await row(db_session, out.tool_call_id)).arguments["weird"] == "1.50"


# --- every call is logged ------------------------------------------------------------------


async def test_every_call_leaves_exactly_one_tool_call_and_one_audit_row(
    db_session, alice, bob
) -> None:
    mine = await create_order(db_session, alice, status=OrderStatus.paid)
    theirs = await create_order(db_session, bob)
    ex = make_executor(admin_ping=(FakeHandler(), ADMIN), asker=(FakeHandler(policy=ask()), BOTH))
    calls = [
        ("get_order", {"order_id": str(mine.id)}),  # success
        ("get_order", {"order_id": str(theirs.id)}),  # not found
        ("get_order", {"order_id": "nope"}),  # invalid
        ("no_such_tool", {}),  # unknown
        ("admin_ping", {}),  # authz denial
        ("cancel_order", {"order_id": str(mine.id)}),  # policy denial
        ("asker", {}),  # awaiting approval
        ("create_ticket", {"subject": "s", "body": "b"}),  # success + outbox
    ]
    before = await count(db_session, ToolCall)
    ctx = ctx_for(alice)
    outcomes = [await ex.run(db_session, ctx, name, args) for name, args in calls]

    assert await count(db_session, ToolCall) - before == len(calls)
    assert len({o.tool_call_id for o in outcomes}) == len(calls)
    for o in outcomes:
        assert len(await audit(db_session, o.tool_call_id)) == 1, o
    assert [o.status.value for o in outcomes] == [
        "succeeded",
        "failed",
        "invalid",
        "invalid",
        "denied",
        "denied",
        "awaiting_approval",
        "succeeded",
    ]


# --- outbox atomicity through the executor -------------------------------------------------


async def test_ticket_created_event_is_written_with_the_ticket(db_session, alice) -> None:
    out = await make_executor().run(
        db_session,
        ctx_for(alice, request_id="req-abcdef12"),
        "create_ticket",
        {"subject": "Hi", "body": "b"},
    )
    ticket = await db_session.scalar(select(Ticket))
    event = await db_session.scalar(select(OutboxEvent))
    assert out.status == ToolCallStatus.succeeded
    assert (event.event_type, event.aggregate_type, event.aggregate_id) == (
        "ticket.created",
        "ticket",
        str(ticket.id),
    )
    assert event.published_at is None and event.attempts == 0
    assert event.payload["data"]["user_id"] == str(alice.id)
    assert event.payload["data"]["priority"] == "normal"
    assert "subject" not in event.payload["data"]  # ids and state only: no free text
    assert event.payload["meta"]["request_id"] == "req-abcdef12"
