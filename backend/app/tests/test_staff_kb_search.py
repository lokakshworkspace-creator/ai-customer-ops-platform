"""The staff knowledge-base lookup: GET /api/v1/admin/knowledge/search.

An administrator can read INTERNAL documents through it; nobody else can, and the assistant still
cannot (its tool is hard-wired to customer documents). Real auth, real retriever and database; the
embedder is the deterministic fake, so these tests check who may read what and how it is logged,
not retrieval quality."""

import json

import pytest
from sqlalchemy import select

from app.api.deps import get_session
from app.api.routers.admin import get_kb_embedder
from app.db.models import AuditLog
from app.db.models.enums import KbAudience, Role
from app.main import app
from app.rag.audience import audiences_for_role
from app.rag.embedder import EmbedderError, FakeEmbedder
from app.tests.test_rag_retriever import load
from app.tests.tokens import auth_for, bearer, forge_token
from app.tests.worlds import World, make_world
from app.tools.handlers.knowledge import AGENT_AUDIENCES

URL = "/api/v1/admin/knowledge/search"
CODES = "AMBER-HOLD RED-STOP escalation codes"


@pytest.fixture
async def kb(db_session) -> None:
    await load(db_session)  # first: ingest ends with a rollback, which would undo a pending world


@pytest.fixture
async def world(db_session, kb) -> World:
    w = await make_world(db_session)
    await db_session.commit()  # the endpoint ends its read transaction, as a real request would
    return w


class Counting(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__(768)
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return await super().embed_query(text)


@pytest.fixture
def embedder():
    emb = Counting()
    app.dependency_overrides[get_kb_embedder] = lambda: emb
    yield emb
    app.dependency_overrides.pop(get_kb_embedder, None)


# --- who may ask ------------------------------


async def test_a_customer_gets_403_before_anything_is_embedded_or_searched(
    refund_client, world, embedder, db_session
) -> None:
    r = await refund_client.get(URL, params={"q": CODES}, headers=auth_for(world.alice.id))
    assert r.status_code == 403 and "results" not in r.json()
    assert embedder.queries == []  # not even the embedding API was called
    assert (
        list(await db_session.scalars(select(AuditLog).where(AuditLog.action == "admin.kb_search")))
        == []
    )


async def test_a_forged_admin_claim_in_a_customers_token_is_still_403(
    refund_client, world, embedder
) -> None:
    token = forge_token(world.alice.id, extra={"role": "admin"})
    r = await refund_client.get(URL, params={"q": CODES}, headers=bearer(token))
    assert r.status_code == 403 and embedder.queries == []


async def test_without_a_login_it_is_401(refund_client, world, embedder) -> None:
    assert (await refund_client.get(URL, params={"q": CODES})).status_code == 401


async def test_the_audience_cannot_be_chosen_by_the_request(refund_client, world, embedder) -> None:
    h = auth_for(world.alice.id)
    for extra in ({"audience": "internal"}, {"audiences": "internal"}, {"role": "admin"}):
        r = await refund_client.get(URL, params={"q": CODES, **extra}, headers=h)
        assert r.status_code == 403  # a customer stays a customer, whatever she asks for


def test_the_role_to_audience_mapping_is_one_function_and_the_agent_does_not_use_it() -> None:
    assert audiences_for_role(Role.admin) == (KbAudience.customer, KbAudience.internal)
    assert audiences_for_role(Role.customer) == (KbAudience.customer,)
    assert AGENT_AUDIENCES == (KbAudience.customer,)  # the assistant: customer-only, for everyone
    assert AGENT_AUDIENCES != audiences_for_role(Role.admin)


# --- what an administrator gets ------------------------------


async def test_an_admin_can_read_internal_documents_and_sees_which_ones_they_are(
    refund_client, world, embedder
) -> None:
    r = await refund_client.get(URL, params={"q": CODES}, headers=auth_for(world.admin.id))
    assert r.status_code == 200 and r.json()["found"] is True
    results = r.json()["results"]
    assert results[0]["audience"] == "internal" and results[0]["title"].startswith("Internal")
    assert "AMBER-HOLD" in json.dumps(results)
    assert set(results[0]) == {"title", "audience", "heading", "content", "score"}
    assert embedder.queries == [CODES]


async def test_an_admin_also_finds_customer_documents_and_they_are_labelled(
    refund_client, world, embedder
) -> None:
    r = await refund_client.get(
        URL, params={"q": "how long does shipping take"}, headers=auth_for(world.admin.id)
    )
    audiences = {x["audience"] for x in r.json()["results"]}
    assert "customer" in audiences and r.json()["results"][0]["title"] == "Shipping and delivery"


async def test_a_question_with_no_answer_is_found_false(refund_client, world, embedder) -> None:
    r = await refund_client.get(
        URL, params={"q": "explain quantum entanglement"}, headers=auth_for(world.admin.id)
    )
    assert r.status_code == 200 and r.json() == {"found": False, "results": []}


@pytest.mark.parametrize("q", ["", "x", " " * 5, "q" * 501])
async def test_a_bad_question_is_422(refund_client, world, embedder, q) -> None:
    r = await refund_client.get(URL, params={"q": q}, headers=auth_for(world.admin.id))
    assert r.status_code == 422 and embedder.queries == []


async def test_a_missing_question_is_422(refund_client, world, embedder) -> None:
    assert (await refund_client.get(URL, headers=auth_for(world.admin.id))).status_code == 422


# --- the audit trail ------------------------------


async def test_every_search_is_audited_without_storing_the_question(
    refund_client, world, embedder, db_session
) -> None:
    await refund_client.get(URL, params={"q": CODES}, headers=auth_for(world.admin.id))
    (row,) = await db_session.scalars(select(AuditLog).where(AuditLog.action == "admin.kb_search"))
    assert row.actor_user_id == world.admin.id and row.resource_type == "knowledge_base"
    assert row.details["query_chars"] == len(CODES)
    assert row.details["results"] >= 1 and row.details["internal_results"] >= 1
    assert row.details["audiences"] == ["customer", "internal"]
    assert "AMBER" not in json.dumps(row.details) and CODES not in json.dumps(row.details)


# --- failures and transactions ------------------------------


async def test_an_embedding_outage_is_a_generic_503_with_no_detail(
    refund_client, world, embedder
) -> None:
    class Down(FakeEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            raise EmbedderError("rate limited, key=SECRETKEY123")

    app.dependency_overrides[get_kb_embedder] = lambda: Down(768)
    r = await refund_client.get(URL, params={"q": CODES}, headers=auth_for(world.admin.id))
    assert r.status_code == 503 and r.json()["code"] == "KB_UNAVAILABLE"
    assert "SECRETKEY" not in r.text and "rate limited" not in r.text


async def test_a_dimension_mismatch_is_a_loud_generic_503(refund_client, world) -> None:
    app.dependency_overrides[get_kb_embedder] = lambda: FakeEmbedder(512)
    try:
        r = await refund_client.get(URL, params={"q": CODES}, headers=auth_for(world.admin.id))
    finally:
        app.dependency_overrides.pop(get_kb_embedder, None)
    assert r.status_code == 503 and r.json()["code"] == "KB_UNAVAILABLE" and "512" not in r.text


async def test_no_transaction_is_open_while_the_question_is_embedded(
    refund_client, world, db_session
) -> None:
    seen: list[bool] = []

    class Probe(FakeEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            seen.append(db_session.in_transaction())
            return await super().embed_query(text)

    app.dependency_overrides[get_kb_embedder] = lambda: Probe(768)
    try:
        r = await refund_client.get(URL, params={"q": CODES}, headers=auth_for(world.admin.id))
    finally:
        app.dependency_overrides.pop(get_kb_embedder, None)
    assert r.status_code == 200 and seen == [False]
    assert get_session in app.dependency_overrides  # (the fixture's own override is intact)
