"""Migration 0003: chat messages, conversation / message identity on workflow_runs."""

import asyncio
import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models import Message, WorkflowRun
from app.db.models.enums import MessageRole, WorkflowStatus
from app.db.models.message import MAX_CONTENT_BYTES
from app.tests.conftest import _alembic_config, _test_db_url, rebuild_schema, truncate_all
from app.tests.factories import create_user


async def run_with_user(session: AsyncSession, **kw) -> WorkflowRun:
    user = kw.pop("user", None) or await create_user(session)
    run = WorkflowRun(user_id=user.id, state="CLASSIFY", **kw)
    session.add(run)
    await session.flush()
    return run


async def rejected(session: AsyncSession, statement: str, **params) -> bool:
    try:
        async with session.begin_nested():
            await session.execute(text(statement), params)
    except IntegrityError:
        return True
    return False


# --- messages ---------------------------------------------------------------------------------


async def test_messages_are_ordered_by_an_identity_seq(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    conversation = uuid.uuid4()
    for i in range(3):
        db_session.add(
            Message(
                conversation_id=conversation,
                user_id=user.id,
                role=MessageRole.user,
                content=[{"type": "text", "text": str(i)}],
            )
        )
    await db_session.flush()
    rows = (
        await db_session.execute(
            text("SELECT seq, content->0->>'text' AS body FROM messages ORDER BY seq")
        )
    ).all()
    assert [r.body for r in rows] == ["0", "1", "2"] and len({r.seq for r in rows}) == 3


async def test_message_role_and_size_are_checked(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    base = dict(c=uuid.uuid4(), u=user.id)
    insert = (
        "INSERT INTO messages (id, conversation_id, user_id, role, content) "
        "VALUES (gen_random_uuid(), :c, :u, :r, CAST(:body AS jsonb))"
    )
    assert not await rejected(db_session, insert, r="assistant", body="[]", **base)
    assert await rejected(db_session, insert, r="system", body="[]", **base)
    big = '[{"type":"text","text":"' + "x" * MAX_CONTENT_BYTES + '"}]'
    assert await rejected(db_session, insert, r="user", body=big, **base)


async def test_a_message_belongs_to_a_user_and_cannot_orphan_one(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    db_session.add(
        Message(conversation_id=uuid.uuid4(), user_id=user.id, role=MessageRole.user, content=[])
    )
    await db_session.flush()
    assert await rejected(db_session, "DELETE FROM users WHERE id = :u", u=user.id)  # RESTRICT


# --- workflow_runs ------------------------------------------------------------------------------


async def test_escalated_is_a_valid_status_and_unknown_ones_are_not(
    db_session: AsyncSession,
) -> None:
    run = await run_with_user(db_session, status=WorkflowStatus.escalated)
    assert run.status == WorkflowStatus.escalated
    assert await rejected(
        db_session, "UPDATE workflow_runs SET status = 'bogus' WHERE id = :i", i=run.id
    )


async def test_the_same_message_id_cannot_be_used_twice_in_a_conversation(
    db_session: AsyncSession,
) -> None:
    user = await create_user(db_session)
    conversation, message = uuid.uuid4(), uuid.uuid4()
    await run_with_user(
        db_session,
        user=user,
        conversation_id=conversation,
        message_id=message,
        status=WorkflowStatus.completed,
    )
    again = (
        "INSERT INTO workflow_runs (id, user_id, status, state, conversation_id, message_id) "
        "VALUES (gen_random_uuid(), :u, 'completed', 'DONE', :c, :m)"
    )
    assert await rejected(db_session, again, u=user.id, c=conversation, m=message)


async def test_a_message_id_cannot_be_reused_by_the_same_user_in_another_conversation(
    db_session: AsyncSession,
) -> None:
    """Covers a retry of a FIRST message, which has no conversation id yet."""
    user = await create_user(db_session)
    message = uuid.uuid4()
    await run_with_user(
        db_session,
        user=user,
        conversation_id=uuid.uuid4(),
        message_id=message,
        status=WorkflowStatus.completed,
    )
    other = (
        "INSERT INTO workflow_runs (id, user_id, status, state, conversation_id, message_id) "
        "VALUES (gen_random_uuid(), :u, 'completed', 'DONE', gen_random_uuid(), :m)"
    )
    assert await rejected(db_session, other, u=user.id, m=message)
    # a different user may use any message id: ids are per user
    someone = await create_user(db_session)
    assert not await rejected(db_session, other, u=someone.id, m=message)


async def test_runs_that_are_not_chat_turns_are_unaffected_by_the_new_indexes(
    db_session: AsyncSession,
) -> None:
    user = await create_user(db_session)
    for _ in range(3):  # NULL conversation_id / message_id never collide
        await run_with_user(db_session, user=user, status=WorkflowStatus.running)


async def test_at_most_one_running_turn_per_conversation(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    conversation = uuid.uuid4()
    first = await run_with_user(
        db_session,
        user=user,
        conversation_id=conversation,
        message_id=uuid.uuid4(),
        status=WorkflowStatus.running,
    )
    insert = (
        "INSERT INTO workflow_runs (id, user_id, status, state, conversation_id, message_id) "
        "VALUES (gen_random_uuid(), :u, :s, 'X', :c, gen_random_uuid())"
    )
    assert await rejected(db_session, insert, u=user.id, s="running", c=conversation)
    # a turn waiting for a human does not block the next message
    assert not await rejected(db_session, insert, u=user.id, s="waiting_approval", c=conversation)
    # and once the first turn is over, a new one may run
    first.status = WorkflowStatus.completed
    await db_session.flush()
    assert not await rejected(db_session, insert, u=user.id, s="running", c=conversation)


# --- round trip ----------------------------------------------------------------------------------


async def _alembic(engine: AsyncEngine, fn, *args) -> None:
    await asyncio.to_thread(fn, _alembic_config(_test_db_url()), *args)
    await engine.dispose()


async def test_downgrade_maps_escalated_to_failed_and_upgrade_restores_the_schema(
    engine: AsyncEngine,
) -> None:
    await truncate_all(engine)
    try:
        user = uuid.uuid4()
        async with engine.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, full_name, role) "
                    "VALUES (:u, 'chat@example.com', 'x', 'Chat', 'customer')"
                ),
                {"u": user},
            )
            await c.execute(
                text(
                    "INSERT INTO workflow_runs (id, user_id, status, state) "
                    "VALUES (gen_random_uuid(), :u, 'escalated', 'ESCALATED')"
                ),
                {"u": user},
            )
        await _alembic(engine, command.downgrade, "0002")
        async with engine.connect() as c:
            assert await c.scalar(text("SELECT status FROM workflow_runs")) == "failed"
            assert await c.scalar(text("SELECT to_regclass('messages')")) is None
        await _alembic(engine, command.upgrade, "head")
        async with engine.connect() as c:
            assert await c.scalar(text("SELECT count(*) FROM messages")) == 0
            assert await c.scalar(text("SELECT status FROM workflow_runs")) == "failed"
    finally:
        await rebuild_schema(engine)


@pytest.mark.parametrize("name", ["ck_messages_message_role", "ck_messages_content_size"])
async def test_each_check_exists_exactly_once(db_session: AsyncSession, name: str) -> None:
    n = await db_session.scalar(
        text("SELECT count(*) FROM pg_constraint WHERE conname = :n"), {"n": name}
    )
    assert n == 1
    status_checks = await db_session.scalar(
        text(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'workflow_runs'::regclass "
            "AND contype = 'c'"
        )
    )
    assert status_checks == 1  # the one workflow_status CHECK, not duplicated
