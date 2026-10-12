"""Schema-level guarantees, tested against the real migrated Postgres."""

import asyncio
import re
import uuid
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.base import Base
from app.db.models import Approval, Order, OutboxEvent, ProcessedEvent, ToolCall, User
from app.db.models.enums import (
    ApprovalStatus,
    OrderStatus,
    PaymentStatus,
    PolicyDecision,
    RefundStatus,
    Role,
    TicketPriority,
    TicketStatus,
    ToolCallStatus,
    WorkflowStatus,
)
from app.db.models.tool_call import MAX_ARGUMENTS_BYTES, MAX_RESULT_BYTES
from app.tests.conftest import _alembic_config, _test_db_url
from app.tests.factories import create_order, create_payment, create_refund, create_user


async def test_pgvector_extension_is_enabled(db_session: AsyncSession) -> None:
    name = await db_session.scalar(
        text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    )
    assert name == "vector"


async def test_all_model_tables_exist(db_session: AsyncSession) -> None:
    rows = await db_session.scalars(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    existing = set(rows)
    assert set(Base.metadata.tables) <= existing
    assert len(Base.metadata.tables) == 16


async def test_migration_matches_models(engine: AsyncEngine) -> None:
    """`alembic check` fails if the models have drifted from the migrations."""
    await asyncio.to_thread(command.check, _alembic_config(_test_db_url()))


def test_uuid_primary_key_is_generated_app_side() -> None:
    col = User.__table__.c.id
    assert col.server_default is None  # not the database
    assert col.default is not None
    assert isinstance(col.default.arg(None), uuid.UUID)


async def test_user_email_is_unique(db_session: AsyncSession) -> None:
    await create_user(db_session, email="dup@example.com")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await create_user(db_session, email="dup@example.com")


async def test_money_roundtrips_as_exact_decimal(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    order = await create_order(db_session, user, total="0.10", items=[("S", 1, "0.10")])
    order_id = order.id
    db_session.expire_all()  # force a real round trip through the database
    loaded = await db_session.scalar(select(Order).where(Order.id == order_id))
    assert loaded is not None
    assert loaded.total_amount == Decimal("0.10")
    assert isinstance(loaded.total_amount, Decimal)


async def test_money_check_constraints(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await create_order(db_session, user, total="-1.00")
    order = await create_order(db_session, user)
    with pytest.raises(IntegrityError):  # amount must be > 0
        async with db_session.begin_nested():
            await create_payment(db_session, order, amount="0.00")


async def test_status_check_constraint_rejects_unknown_value(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, total_amount, currency) "
                    "VALUES (:id, :uid, 'bogus', 1.00, 'INR')"
                ),
                {"id": uuid.uuid4(), "uid": user.id},
            )


async def test_refund_idempotency_key_is_unique_per_user(db_session: AsyncSession) -> None:
    alice = await create_user(db_session)
    bob = await create_user(db_session)
    a_pay = await create_payment(db_session, await create_order(db_session, alice))
    b_pay = await create_payment(db_session, await create_order(db_session, bob))

    await create_refund(db_session, a_pay, alice, idempotency_key="key-1")
    with pytest.raises(IntegrityError) as exc:
        async with db_session.begin_nested():
            await create_refund(db_session, a_pay, alice, idempotency_key="key-1")
    assert "uq_refunds_requested_by_idempotency_key" in str(exc.value)

    # The same key from a *different user* is a different key.
    await create_refund(db_session, b_pay, bob, idempotency_key="key-1")
    # A different key for the same user is fine.
    await create_refund(db_session, a_pay, alice, idempotency_key="key-2")


@pytest.mark.parametrize("column", ["idempotency_key", "request_hash", "reason_code", "status"])
async def test_refund_required_columns_are_not_null(db_session: AsyncSession, column: str) -> None:
    admin = await create_user(db_session, role=Role.admin)
    order = await create_order(db_session, admin)
    payment = await create_payment(db_session, order)
    values = {
        "idempotency_key": "'k'",
        "request_hash": "'h'",
        "reason_code": "'damaged'",
        "status": "'processing'",
    }
    values[column] = "NULL"
    with pytest.raises(IntegrityError) as exc:
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO refunds (id, idempotency_key, request_hash, reason_code, status,"
                    " payment_id, order_id, requested_by, amount, currency)"
                    f" VALUES (:id, {values['idempotency_key']}, {values['request_hash']},"
                    f" {values['reason_code']}, {values['status']}, :p, :o, :u, 5.00, 'INR')"
                ),
                {"id": uuid.uuid4(), "p": payment.id, "o": order.id, "u": admin.id},
            )
    assert column in str(exc.value)


@pytest.mark.parametrize("old_or_unknown", ["pending", "approved", "bogus"])
async def test_refund_status_check_rejects_values_outside_the_new_lifecycle(
    db_session: AsyncSession, old_or_unknown: str
) -> None:
    admin = await create_user(db_session, role=Role.admin)
    payment = await create_payment(db_session, await create_order(db_session, admin))
    refund = await create_refund(db_session, payment, admin, idempotency_key="k")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE refunds SET status = :s WHERE id = :id"),
                {"s": old_or_unknown, "id": refund.id},
            )


async def test_only_one_pending_approval_per_refund(db_session: AsyncSession) -> None:
    admin = await create_user(db_session, role=Role.admin)
    payment = await create_payment(db_session, await create_order(db_session, admin))
    refund = await create_refund(
        db_session, payment, admin, idempotency_key="k", status=RefundStatus.pending_approval
    )

    def approval(status: ApprovalStatus) -> Approval:
        return Approval(refund_id=refund.id, requested_by=admin.id, action="refund", status=status)

    db_session.add(approval(ApprovalStatus.pending))
    await db_session.flush()
    with pytest.raises(IntegrityError) as exc:
        async with db_session.begin_nested():
            db_session.add(approval(ApprovalStatus.pending))
            await db_session.flush()
    assert "uq_approvals_refund_id_pending" in str(exc.value)

    # Decided approvals don't count: history can accumulate.
    db_session.add(approval(ApprovalStatus.rejected))
    db_session.add(approval(ApprovalStatus.rejected))
    await db_session.flush()


async def test_tool_call_status_invalid_is_allowed_and_unknown_is_not(
    db_session: AsyncSession,
) -> None:
    user = await create_user(db_session)
    db_session.add(ToolCall(user_id=user.id, tool_name="nope", status=ToolCallStatus.invalid))
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE tool_calls SET status = 'bogus' WHERE user_id = :u"), {"u": user.id}
            )


async def test_tool_call_stored_size_is_capped(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    ok = ToolCall(
        user_id=user.id, tool_name="t", arguments={"x": "a" * (MAX_ARGUMENTS_BYTES - 100)}
    )
    db_session.add(ok)
    await db_session.flush()

    for field, limit in (("arguments", MAX_ARGUMENTS_BYTES), ("result", MAX_RESULT_BYTES)):
        with pytest.raises(IntegrityError) as exc:
            async with db_session.begin_nested():
                db_session.add(
                    ToolCall(user_id=user.id, tool_name="t", **{field: {"x": "a" * limit}})
                )
                await db_session.flush()
        assert f"{field}_size" in str(exc.value)


async def test_outbox_seq_is_strictly_increasing_even_within_one_transaction(
    db_session: AsyncSession,
) -> None:
    events = [
        OutboxEvent(aggregate_type="refund", aggregate_id="r1", event_type=f"e{i}")
        for i in range(3)
    ]
    db_session.add_all(events)
    await db_session.flush()
    for e in events:
        await db_session.refresh(e)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    # created_at ties inside one transaction (that is why `seq` exists).
    assert len({e.created_at for e in events}) == 1


async def test_outbox_defaults(db_session: AsyncSession) -> None:
    e = OutboxEvent(aggregate_type="refund", aggregate_id="r1", event_type="refund.requested")
    db_session.add(e)
    await db_session.flush()
    await db_session.refresh(e)
    assert (e.attempts, e.published_at, e.parked_at, e.last_error) == (0, None, None, None)
    assert e.next_attempt_at is not None


async def test_processed_events_ledger_dedupes_per_consumer(db_session: AsyncSession) -> None:
    event_id = uuid.uuid4()
    db_session.add(ProcessedEvent(consumer="notifications", event_id=event_id))
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(ProcessedEvent(consumer="notifications", event_id=event_id))
            await db_session.flush()
    # A different consumer may process the same event.
    db_session.add(ProcessedEvent(consumer="analytics", event_id=event_id))
    await db_session.flush()


ENUM_COLUMNS = [
    ("users", "role", Role),
    ("orders", "status", OrderStatus),
    ("payments", "status", PaymentStatus),
    ("refunds", "status", RefundStatus),
    ("tickets", "status", TicketStatus),
    ("tickets", "priority", TicketPriority),
    ("workflow_runs", "status", WorkflowStatus),
    ("approvals", "status", ApprovalStatus),
    ("tool_calls", "status", ToolCallStatus),
    ("tool_calls", "policy_decision", PolicyDecision),
    ("audit_logs", "decision", PolicyDecision),
]

_CHECKS_ON_COLUMN = text(
    "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
    "JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (c.conkey) "
    "WHERE c.contype = 'c' AND t.relname = :t AND a.attname = :c AND cardinality(c.conkey) = 1"
)


@pytest.mark.parametrize(("table", "column", "enum_cls"), ENUM_COLUMNS)
async def test_every_enum_column_has_exactly_one_check_matching_the_python_enum(
    db_session: AsyncSession, table: str, column: str, enum_cls: type
) -> None:
    """Closes the gap that `alembic check` does not compare CHECK constraints, and guards
    against the autogenerate duplicate-CHECK gotcha."""
    rows = list((await db_session.execute(_CHECKS_ON_COLUMN, {"t": table, "c": column})).scalars())
    assert len(rows) == 1, f"{table}.{column}: expected exactly one single-column CHECK, got {rows}"
    db_values = set(re.findall(r"'([^']+)'", rows[0]))
    assert db_values == {m.value for m in enum_cls}, f"{table}.{column} CHECK drifted from enum"


async def test_deleting_order_with_payment_is_restricted(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    order = await create_order(db_session, user)
    await create_payment(db_session, order)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text("DELETE FROM orders WHERE id = :id"), {"id": order.id})
