"""GET /api/v1/admin/pipeline: is the background machinery doing its job? Admin only, read-only,
counts and ages only, and nothing to do with /health."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.db.models import AuditLog, OutboxEvent, ToolCall
from app.tests.test_approvals_api import decide, pending
from app.tests.tokens import auth_for, bearer, forge_token
from app.tests.worlds import World, make_world

URL = "/api/v1/admin/pipeline"


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


async def get(client, who) -> dict:
    r = await client.get(URL, headers=auth_for(who.id))
    assert r.status_code == 200, r.text
    return r.json()


async def add_outbox(db_session, *, age_s: float, parked: bool = False, published: bool = False):
    created = datetime.now(UTC) - timedelta(seconds=age_s)
    db_session.add(
        OutboxEvent(
            aggregate_type="refund",
            aggregate_id=str(uuid.uuid4()),
            event_type="refund.requested",
            payload={"data": {}, "meta": {}},
            created_at=created,
            published_at=created if published else None,
            parked_at=created if parked else None,
            attempts=3 if parked else 0,
        )
    )
    await db_session.flush()


async def wait_turn(db_session, world: World, refund_id: str, approval_id: str) -> None:
    """A chat turn waiting for that approval, as the orchestrator leaves it."""
    await db_session.execute(
        text(
            "INSERT INTO workflow_runs (id, user_id, status, state, context) "
            "VALUES (gen_random_uuid(), :u, 'waiting_approval', 'WAITING_FOR_HUMAN', "
            "CAST(:c AS jsonb))"
        ),
        {
            "u": world.alice.id,
            "c": json.dumps({"refund_id": refund_id, "approval_id": approval_id}),
        },
    )


async def age_decision(db_session, approval_id: str, refund_id: str, seconds: int) -> None:
    ago = datetime.now(UTC) - timedelta(seconds=seconds)
    await db_session.execute(
        text("UPDATE approvals SET decided_at = :t WHERE id = :i"),
        {"t": ago, "i": uuid.UUID(approval_id)},
    )
    await db_session.execute(
        text("UPDATE refunds SET processed_at = :t WHERE id = :i"),
        {"t": ago, "i": uuid.UUID(refund_id)},
    )


# --- who may look -----------------------------------------------------------------------


async def test_an_admin_gets_a_snapshot_and_an_idle_system_is_ok(refund_client, world) -> None:
    body = await get(refund_client, world.admin)
    assert body["status"] == "ok" and body["attention"] == []
    assert body["outbox"] == {
        "unpublished": 0,
        "retrying": 0,
        "parked": 0,
        "oldest_unpublished_age_s": None,
    }
    assert body["waiting_turns"]["waiting"] == 0
    assert body["approvals"]["pending"] == 0 and body["approvals"]["overdue"] == 0
    assert body["thresholds"]["approval_ttl_hours"] == 48


async def test_a_customer_gets_403_and_nothing(refund_client, world) -> None:
    r = await refund_client.get(URL, headers=auth_for(world.alice.id))
    assert r.status_code == 403 and "outbox" not in r.json()
    forged = bearer(forge_token(world.alice.id, extra={"role": "admin"}))
    assert (await refund_client.get(URL, headers=forged)).status_code == 403


async def test_it_needs_a_login(refund_client) -> None:
    assert (await refund_client.get(URL)).status_code == 401


async def test_a_revoked_admin_token_is_refused(refund_client, db_session, world) -> None:
    from app.core.security import create_access_token, decode_token
    from app.modules.users import tokens as users_tokens

    token = create_access_token(world.admin.id)[0]
    claims = decode_token(token)
    await users_tokens.revoke(
        db_session, jti=claims.jti, user_id=claims.user_id, expires_at=claims.expires_at
    )
    assert (await refund_client.get(URL, headers=bearer(token))).status_code == 401


# --- the relay --------------------------------------------------------------------------


async def test_a_fresh_unpublished_event_is_normal(refund_client, db_session, world) -> None:
    await add_outbox(db_session, age_s=5)
    body = await get(refund_client, world.admin)
    assert body["outbox"]["unpublished"] == 1
    assert body["outbox"]["oldest_unpublished_age_s"] in range(5, 15)
    assert body["status"] == "ok"


async def test_an_old_unpublished_event_means_the_relay_is_not_publishing(
    refund_client, db_session, world
) -> None:
    await add_outbox(db_session, age_s=300)
    await add_outbox(db_session, age_s=10)
    await add_outbox(db_session, age_s=999, published=True)  # published rows are history
    body = await get(refund_client, world.admin)
    assert body["outbox"]["unpublished"] == 2
    assert body["outbox"]["oldest_unpublished_age_s"] in range(300, 310)
    assert body["status"] == "attention" and "outbox_lagging" in body["attention"]


async def test_a_parked_event_is_reported_separately(refund_client, db_session, world) -> None:
    await add_outbox(db_session, age_s=5, parked=True)
    body = await get(refund_client, world.admin)
    assert body["outbox"]["parked"] == 1 and body["outbox"]["unpublished"] == 0
    assert "outbox_parked" in body["attention"]


# --- the resume consumer ----------------------------------------------------------------


async def test_a_turn_still_waiting_for_a_human_is_normal(refund_client, db_session, world) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await wait_turn(db_session, world, refund_id, approval_id)
    body = await get(refund_client, world.admin)
    assert body["waiting_turns"]["waiting"] == 1
    assert body["waiting_turns"]["decided"] == 0
    assert body["status"] == "ok"


async def test_a_decision_the_consumer_has_had_no_time_for_yet_is_not_alarming(
    refund_client, db_session, world
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await wait_turn(db_session, world, refund_id, approval_id)
    assert (await decide(refund_client, world.admin, approval_id, "reject")).status_code == 200
    body = await get(refund_client, world.admin)
    assert body["waiting_turns"]["decided"] == 1  # final, and the turn still waits ...
    assert body["waiting_turns"]["decided_not_finished"] == 0  # ... but only for a moment
    assert body["status"] == "ok"


@pytest.mark.parametrize("verb", ["reject", "approve"])
async def test_a_turn_still_waiting_long_after_a_final_decision_means_the_consumer_is_down(
    refund_client, db_session, world, verb: str
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await wait_turn(db_session, world, refund_id, approval_id)
    assert (await decide(refund_client, world.admin, approval_id, verb)).status_code == 200
    await age_decision(db_session, approval_id, refund_id, 300)
    body = await get(refund_client, world.admin)
    t = body["waiting_turns"]
    assert t["decided_not_finished"] == 1
    assert t["oldest_decided_not_finished_age_s"] in range(300, 310)
    assert body["status"] == "attention" and "turns_not_finished" in body["attention"]


async def test_a_turn_still_waiting_long_after_an_expiry_means_the_consumer_is_down(
    refund_client, db_session, world
) -> None:
    from app.modules.refunds import expiry

    refund_id, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await wait_turn(db_session, world, refund_id, approval_id)
    await db_session.execute(
        text("UPDATE approvals SET created_at = now() - interval '50 hours' WHERE id = :i"),
        {"i": uuid.UUID(approval_id)},
    )
    assert await expiry.expire_one(db_session, uuid.UUID(approval_id)) is True
    await age_decision(db_session, approval_id, refund_id, 300)
    body = await get(refund_client, world.admin)
    assert body["waiting_turns"]["decided_not_finished"] == 1
    assert "turns_not_finished" in body["attention"]


async def test_an_approved_refund_still_processing_is_not_a_stuck_turn(
    refund_client, db_session, world, provider
) -> None:
    """Waiting for the payout is the designed state (waiting_for: payout), not a dead consumer."""
    refund_id, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await wait_turn(db_session, world, refund_id, approval_id)
    provider.unavailable_next()
    assert (await decide(refund_client, world.admin, approval_id, "approve")).status_code in (
        200,
        202,
    )
    await age_decision(db_session, approval_id, refund_id, 300)
    status = await db_session.scalar(
        text("SELECT status FROM refunds WHERE id = :i"), {"i": uuid.UUID(refund_id)}
    )
    assert status == "processing"
    body = await get(refund_client, world.admin)
    assert (
        body["waiting_turns"]["decided"] == 0 and body["waiting_turns"]["decided_not_finished"] == 0
    )
    assert "turns_not_finished" not in body["attention"]


async def test_a_turn_with_an_unusable_checkpoint_does_not_break_the_snapshot(
    refund_client, db_session, world
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO workflow_runs (id, user_id, status, state, context) VALUES "
            "(gen_random_uuid(), :u, 'waiting_approval', 'WAITING_FOR_HUMAN', "
            'CAST(\'{"approval_id": "not-a-uuid"}\' AS jsonb))'
        ),
        {"u": world.alice.id},
    )
    body = await get(refund_client, world.admin)
    assert body["waiting_turns"]["waiting"] == 1 and body["waiting_turns"]["decided"] == 0


# --- the sweeper ------------------------------------------------------------------------


async def test_pending_approvals_inside_their_limit_are_normal(
    refund_client, db_session, world
) -> None:
    await pending(refund_client, world.alice, world.order, "700.00")
    body = await get(refund_client, world.admin)
    assert body["approvals"]["pending"] == 1 and body["approvals"]["overdue"] == 0
    assert body["status"] == "ok"


async def test_an_approval_long_past_its_deadline_means_the_sweeper_is_not_running(
    refund_client, db_session, world
) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await db_session.execute(
        text("UPDATE approvals SET created_at = now() - interval '50 hours' WHERE id = :i"),
        {"i": uuid.UUID(approval_id)},
    )
    body = await get(refund_client, world.admin)
    assert body["approvals"]["overdue"] == 1
    assert body["status"] == "attention" and "approvals_overdue" in body["attention"]


async def test_an_approval_that_just_passed_its_deadline_is_given_the_sweeper_s_grace(
    refund_client, db_session, world
) -> None:
    _, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await db_session.execute(
        text(
            "UPDATE approvals SET created_at = now() - interval '48 hours' - interval '5 seconds' "
            "WHERE id = :i"
        ),
        {"i": uuid.UUID(approval_id)},
    )
    body = await get(refund_client, world.admin)
    assert body["approvals"]["overdue"] == 1  # it is overdue ...
    assert "approvals_overdue" not in body["attention"]  # ... but the sweeper may just be due


# --- what it is not ---------------------------------------------------------------------


async def test_it_writes_nothing_and_leaks_no_ids_or_emails(
    refund_client, db_session, world
) -> None:
    refund_id, approval_id = await pending(refund_client, world.alice, world.order, "700.00")
    await wait_turn(db_session, world, refund_id, approval_id)

    async def counts():
        return [
            await db_session.scalar(select(func.count()).select_from(m))
            for m in (AuditLog, ToolCall, OutboxEvent)
        ]

    before = await counts()
    r = await refund_client.get(URL, headers=auth_for(world.admin.id))
    assert await counts() == before  # not even an audit row
    for secret in (str(world.alice.id), refund_id, approval_id, "@example.com"):
        assert secret not in r.text


async def test_health_is_untouched_by_the_pipeline_state(refund_client, db_session, world) -> None:
    await add_outbox(db_session, age_s=3600)  # a relay that has been dead for an hour
    r = await refund_client.get("/health")
    assert set(r.json()) == {"status", "postgres", "redis"}  # no pipeline fields in /health
