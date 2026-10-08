"""POST /api/v1/chat and GET /api/v1/workflows/{id}, end to end: the real app, the real
get_current_user, the real executor / policy / database, a fake model."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select

from app.agent.llm.base import LLMConfigError, LLMUnavailable
from app.agent.llm.fake import ObedientFakeLLM, ScriptedLLM, call, say
from app.agent.orchestrator import Orchestrator
from app.api import deps
from app.api.deps import get_executor, get_orchestrator, get_session
from app.db.models import Message, Refund, WorkflowRun
from app.main import app
from app.tests.agent_helpers import make_orchestrator
from app.tests.tokens import auth_for
from app.tests.worlds import Person, World, make_world


class Holder:
    orchestrator: Orchestrator


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


@pytest.fixture
async def holder(db_session) -> Holder:
    h = Holder()
    h.orchestrator = make_orchestrator(ObedientFakeLLM())
    return h


@pytest.fixture
async def client(db_session, holder: Holder) -> AsyncIterator[AsyncClient]:
    async def _session():
        yield db_session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_orchestrator] = lambda: holder.orchestrator
    app.dependency_overrides[get_executor] = lambda: holder.orchestrator.executor
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    for dep in (get_session, get_orchestrator, get_executor):
        app.dependency_overrides.pop(dep, None)


async def say_to(
    client: AsyncClient, who: Person, message: str, *, message_id: Any = None, **extra: Any
) -> Response:
    body = {"message": message, "message_id": str(message_id or uuid.uuid4()), **extra}
    return await client.post("/api/v1/chat", json=body, headers=auth_for(who.id))


def refund_text(world: World, amount: str) -> str:
    return f"my order {world.order} arrived damaged, refund {amount}"


async def n_messages(session) -> int:
    return await session.scalar(select(func.count()).select_from(Message)) or 0


# --- authentication and validation --------------------------------------------------------------


async def test_chat_requires_a_login(client, world) -> None:
    body = {"message": "hi", "message_id": str(uuid.uuid4())}
    assert (await client.post("/api/v1/chat", json=body)).status_code == 401
    assert (await client.get(f"/api/v1/workflows/{uuid.uuid4()}")).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"message": "hi"},  # message_id is required
        {"message": "hi", "message_id": "not-a-uuid"},
        {"message": "", "message_id": "00000000-0000-4000-8000-000000000001"},
        {"message": "   ", "message_id": "00000000-0000-4000-8000-000000000001"},
        {"message": "hi", "message_id": "00000000-0000-4000-8000-000000000001", "role": "admin"},
        {"message": "hi", "message_id": "00000000-0000-4000-8000-000000000001", "user_id": "x"},
        {
            "message": "hi",
            "message_id": "00000000-0000-4000-8000-000000000001",
            "conversation_id": "x",
        },
    ],
)
async def test_invalid_requests_are_422(client, world, body: dict) -> None:
    r = await client.post("/api/v1/chat", json=body, headers=auth_for(world.alice.id))
    assert r.status_code == 422


async def test_a_message_over_the_configured_length_is_422(client, world) -> None:
    r = await say_to(client, world.alice, "x" * 2001)
    assert r.status_code == 422 and r.json()["code"] == "MESSAGE_TOO_LONG"


# --- the happy paths ----------------------------------------------------------------------------


async def test_a_small_refund_through_http(client, db_session, world) -> None:
    r = await say_to(client, world.alice, refund_text(world, "250"))
    assert r.status_code == 200 and "idempotent-replayed" not in r.headers
    j = r.json()
    assert set(j) == {
        "conversation_id",
        "workflow_id",
        "message_id",
        "status",
        "reply",
        "tool_results",
        "llm",
    }
    assert j["status"] == "completed" and j["llm"] == "fake"
    assert [t["tool"] for t in j["tool_results"]] == ["list_orders", "request_refund"]
    assert j["tool_results"][1]["summary"] == "Refund of 250.00 INR: succeeded"
    assert (await db_session.scalar(select(func.count()).select_from(Refund))) == 1


async def test_the_llm_field_names_the_model_kind(client, world, holder) -> None:
    assert (await say_to(client, world.alice, "hello")).json()["llm"] == "fake"
    holder.orchestrator = make_orchestrator(ScriptedLLM(say("hi")))
    holder.orchestrator.llm.name = "claude"  # what a real ClaudeLLMClient reports
    assert (await say_to(client, world.alice, "hello")).json()["llm"] == "claude"


async def test_the_same_message_id_returns_the_stored_response_and_marks_the_replay(
    client, db_session, world, holder
) -> None:
    mid = uuid.uuid4()
    first = await say_to(client, world.alice, refund_text(world, "250"), message_id=mid)
    llm_calls = len(holder.orchestrator.llm.calls)

    again = await say_to(client, world.alice, refund_text(world, "250"), message_id=mid)

    assert again.status_code == 200 and again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert len(holder.orchestrator.llm.calls) == llm_calls
    assert (await db_session.scalar(select(func.count()).select_from(Refund))) == 1


async def test_a_conversation_continues_and_the_model_sees_the_history(
    client, world, holder
) -> None:
    llm = ScriptedLLM(say("first"), say("second"))
    holder.orchestrator = make_orchestrator(llm)
    cid = (await say_to(client, world.alice, "hello")).json()["conversation_id"]
    r = await say_to(client, world.alice, "and again", conversation_id=cid)
    assert r.json()["conversation_id"] == cid
    assert [m.role for m in llm.calls[1].messages] == ["user", "assistant", "user"]


async def test_the_ui_gets_the_truth_even_when_the_model_lies(client, world, holder) -> None:
    args = {"order_id": str(world.order), "amount": "50000.00", "reason_code": "damaged"}
    holder.orchestrator = make_orchestrator(
        ScriptedLLM(
            call("request_refund", **args), say("Great news! Your refund of 50000 was paid.")
        )
    )
    j = (await say_to(client, world.alice, "refund")).json()
    assert "was paid" in j["reply"]  # the model's words...
    assert j["tool_results"][-1]["status"] == "denied"  # ...and what the backend decided
    assert j["tool_results"][-1]["reason_code"] == "AMOUNT_EXCEEDS_ORDER"


# --- errors -------------------------------------------------------------------------------------


async def test_an_unknown_or_foreign_conversation_is_404(client, world) -> None:
    cid = (await say_to(client, world.alice, "hello")).json()["conversation_id"]
    stranger = await say_to(client, world.bob, "hi", conversation_id=cid)
    unknown = await say_to(client, world.bob, "hi", conversation_id=str(uuid.uuid4()))
    assert stranger.status_code == unknown.status_code == 404
    assert (
        stranger.json()
        == unknown.json()
        == {"detail": "Conversation not found", "code": "CONVERSATION_NOT_FOUND"}
    )


async def test_a_model_outage_is_a_clear_503_and_the_message_can_be_resent(
    client, db_session, world, holder
) -> None:
    mid = uuid.uuid4()
    holder.orchestrator = make_orchestrator(
        ScriptedLLM(
            LLMUnavailable(
                "The assistant is temporarily unavailable. Please try again in a moment."
            )
        )
    )
    r = await say_to(client, world.alice, "hello", message_id=mid)
    assert r.status_code == 503 and r.json()["code"] == "ASSISTANT_UNAVAILABLE"
    assert "temporarily unavailable" in r.json()["detail"]
    assert (await db_session.scalar(select(func.count()).select_from(WorkflowRun))) == 0

    holder.orchestrator = make_orchestrator(ScriptedLLM(say("hi")))
    ok = await say_to(client, world.alice, "hello", message_id=mid)  # the same message_id
    assert ok.status_code == 200 and ok.json()["reply"] == "hi"


async def test_a_misconfigured_model_is_503_too(client, world, holder) -> None:
    holder.orchestrator = make_orchestrator(
        ScriptedLLM(
            LLMConfigError("The assistant is not configured correctly. Please contact support.")
        )
    )
    r = await say_to(client, world.alice, "hello")
    assert r.status_code == 503 and "not configured" in r.json()["detail"]


async def test_without_a_key_outside_dev_the_endpoint_says_503_instead_of_faking(
    monkeypatch, world
) -> None:
    def no_model():
        raise LLMConfigError("No LLM is configured: set ANTHROPIC_API_KEY.")

    monkeypatch.setattr(deps, "get_llm_client", no_model)
    with pytest.raises(HTTPException) as caught:
        deps.get_llm()
    assert caught.value.status_code == 503 and "ANTHROPIC_API_KEY" in caught.value.detail


async def test_too_many_messages_in_an_hour_is_429(client, world, holder) -> None:
    holder.orchestrator = make_orchestrator(ObedientFakeLLM(), agent_turns_per_hour=2)
    assert (await say_to(client, world.alice, "one")).status_code == 200
    assert (await say_to(client, world.alice, "two")).status_code == 200
    r = await say_to(client, world.alice, "three")
    assert r.status_code == 429 and r.json()["code"] == "TOO_MANY_TURNS"


async def test_a_message_id_from_another_conversation_is_409(client, world) -> None:
    mid = uuid.uuid4()
    await say_to(client, world.alice, "hello", message_id=mid)
    other = (await say_to(client, world.alice, "hi")).json()["conversation_id"]
    r = await say_to(client, world.alice, "hello", message_id=mid, conversation_id=other)
    assert r.status_code == 409 and r.json()["code"] == "MESSAGE_ID_REUSED"


# --- GET /workflows/{id}: ownership and the waiting-for-approval sync ---------------------------


async def test_a_workflow_is_visible_to_its_owner_only(client, world) -> None:
    wid = (await say_to(client, world.alice, refund_text(world, "250"))).json()["workflow_id"]
    mine = await client.get(f"/api/v1/workflows/{wid}", headers=auth_for(world.alice.id))
    assert mine.status_code == 200 and mine.json()["status"] == "completed"
    assert mine.json()["tool_results"][-1]["summary"] == "Refund of 250.00 INR: succeeded"

    missing = await client.get(
        f"/api/v1/workflows/{uuid.uuid4()}", headers=auth_for(world.alice.id)
    )
    for stranger in (world.bob, world.admin):  # not even an administrator
        theirs = await client.get(f"/api/v1/workflows/{wid}", headers=auth_for(stranger.id))
        assert theirs.status_code == 404 and theirs.json() == missing.json()


async def test_a_waiting_refund_resolves_after_approval_and_two_gets_add_one_message(
    client, db_session, world
) -> None:
    j = (await say_to(client, world.alice, refund_text(world, "700"))).json()
    assert j["status"] == "waiting_approval" and "approv" in j["reply"].lower()
    wid = j["workflow_id"]
    url = f"/api/v1/workflows/{wid}"
    h = auth_for(world.alice.id)

    still = (await client.get(url, headers=h)).json()
    assert still["status"] == "waiting_approval" and still["waiting_for_approval"] is True

    pending = await client.get("/api/v1/approvals", headers=auth_for(world.admin2.id))
    approval_id = pending.json()["items"][0]["id"]
    ok = await client.post(
        f"/api/v1/approvals/{approval_id}/approve", json={}, headers=auth_for(world.admin2.id)
    )
    assert ok.status_code == 200

    before = await n_messages(db_session)
    first = (await client.get(url, headers=h)).json()
    second = (await client.get(url, headers=h)).json()
    third = (await client.get(url, headers=h)).json()

    assert first["status"] == "completed" and first["waiting_for_approval"] is False
    assert "approved and has been paid" in first["latest_message"]
    assert first == second == third  # nothing changes on later reads
    assert await n_messages(db_session) == before + 1  # ONE message, however often it is read
    assert first["reply"] == j["reply"]  # the original reply is kept as it was sent


async def test_a_rejected_refund_is_reported_as_rejected(client, world) -> None:
    j = (await say_to(client, world.alice, refund_text(world, "700"))).json()
    approval_id = (await client.get("/api/v1/approvals", headers=auth_for(world.admin2.id))).json()[
        "items"
    ][0]["id"]
    await client.post(
        f"/api/v1/approvals/{approval_id}/reject", json={}, headers=auth_for(world.admin2.id)
    )
    out = (
        await client.get(f"/api/v1/workflows/{j['workflow_id']}", headers=auth_for(world.alice.id))
    ).json()
    assert out["status"] == "completed" and "did not approve your refund" in out["latest_message"]


# --- concurrency: real connections, real commits ------------------------------------------------


@pytest.fixture
async def committed_chat(committed_db) -> AsyncIterator[tuple[AsyncClient, Holder]]:
    h = Holder()
    h.orchestrator = make_orchestrator(ObedientFakeLLM())

    async def _session():
        async with committed_db.maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_orchestrator] = lambda: h.orchestrator
    app.dependency_overrides[get_executor] = lambda: h.orchestrator.executor
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, h
    for dep in (get_session, get_orchestrator, get_executor):
        app.dependency_overrides.pop(dep, None)


async def test_the_same_message_sent_six_times_at_once_makes_exactly_one_refund(
    committed_db, committed_chat
) -> None:
    import asyncio

    client, _ = committed_chat
    async with committed_db.maker() as s:
        world = await make_world(s)
        await s.commit()
    mid = uuid.uuid4()

    rs = await asyncio.gather(
        *[say_to(client, world.alice, refund_text(world, "250"), message_id=mid) for _ in range(6)]
    )

    assert {r.status_code for r in rs} <= {200, 409}  # 409 = "still being processed", retry later
    fresh = [r for r in rs if r.status_code == 200 and "idempotent-replayed" not in r.headers]
    assert len(fresh) == 1 and fresh[0].json()["status"] == "completed"
    assert {r.json()["workflow_id"] for r in rs if r.status_code == 200} == {
        fresh[0].json()["workflow_id"]
    }
    async with committed_db.maker() as s:
        assert (await s.scalar(select(func.count()).select_from(Refund))) == 1
        assert (await s.scalar(select(func.count()).select_from(WorkflowRun))) == 1


async def test_ten_different_messages_at_once_cannot_refund_more_than_the_order_is_worth(
    committed_db, committed_chat
) -> None:
    import asyncio

    client, _ = committed_chat
    async with committed_db.maker() as s:
        world = await make_world(s)
        await s.commit()

    # ten separate conversations, each asking for 400.00 of a 1000.00 order
    rs = await asyncio.gather(
        *[say_to(client, world.alice, refund_text(world, "400")) for _ in range(10)]
    )

    assert all(r.status_code == 200 for r in rs)
    outcomes = sorted(r.json()["tool_results"][-1]["status"] for r in rs)
    assert outcomes == ["denied"] * 8 + ["succeeded"] * 2
    async with committed_db.maker() as s:
        total = await s.scalar(select(func.coalesce(func.sum(Refund.amount), 0)))
        assert total == 800
