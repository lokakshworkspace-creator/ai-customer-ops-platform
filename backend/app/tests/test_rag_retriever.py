"""The retriever against a real index (fake embedder: these tests check PLUMBING, the audience
filter, thresholds, namespaces and model isolation, not how good real embeddings are)."""

import pytest
from sqlalchemy import text

from app.db.models.enums import KbAudience
from app.policy.rules import get_rules
from app.rag.embedder import FakeEmbedder
from app.rag.guards import KbNotIndexed
from app.rag.ingest import apply_plan, build_plan
from app.rag.kb import load_documents
from app.rag.retriever import RetrievalParams, ensure_ready, keyword_query, search

DOCS = load_documents(get_rules())
EMB = FakeEmbedder(768)
CUSTOMER = [KbAudience.customer]
BOTH = [KbAudience.customer, KbAudience.internal]


async def load(session, ns="default", embedder=EMB, docs=DOCS) -> None:
    plan = await build_plan(session, docs, namespace=ns, model=embedder.model, dim=embedder.dim)
    await apply_plan(session, plan, embedder)


async def ask(session, question, audiences=CUSTOMER, mode="hybrid", ns="default", model=None, **kw):
    kw = {"min_cosine": EMB.default_min_cosine, "min_ts_rank": EMB.default_min_ts_rank, **kw}
    params = RetrievalParams(**kw)
    return await search(
        session,
        query_vec=await EMB.embed_query(question),
        query_text=question,
        audiences=audiences,
        namespace=ns,
        model=model or EMB.model,
        params=params,
        mode=mode,
    )


@pytest.fixture
async def kb(db_session):
    await load(db_session)
    return db_session


# --- it finds the right place ------------------------------


@pytest.mark.parametrize(
    ("question", "title"),
    [
        ("how long does shipping take", "Shipping and delivery"),
        ("can I cancel an order that I have not paid for", "Cancelling an order"),
        ("what payment methods do you accept", "Payments and payment methods"),
        ("how long is the warranty on electronics", "Warranty"),
        ("what are the support hours", "Contacting support and order status"),
    ],
)
async def test_the_right_document_is_found_in_every_mode_that_can_find_it(kb, question, title):
    hybrid = await ask(kb, question)
    assert hybrid and hybrid[0].doc_title == title
    for mode in ("vector", "keyword"):  # plumbing: each leg works alone (thresholds off)
        hits = await ask(kb, question, mode=mode, min_cosine=-1, min_ts_rank=0)
        assert any(h.doc_title == title for h in hits), mode


async def test_hits_carry_title_heading_content_and_scores(kb) -> None:
    (top, *_) = await ask(kb, "return policy for electronics")
    assert top.doc_title == "Returns policy" and top.heading.startswith("Returns policy")
    assert "Electronics" in top.content or "electronics" in top.content
    assert top.cosine is not None and top.cosine > 0.25 and top.rrf > 0


async def test_results_are_ordered_by_fused_rank_and_limited_to_top_k(kb) -> None:
    hits = await ask(kb, "refund returns shipping order", top_k=3, min_cosine=-1, min_ts_rank=0)
    assert len(hits) == 3 and [h.rrf for h in hits] == sorted((h.rrf for h in hits), reverse=True)
    assert len(await ask(kb, "refund returns shipping order", top_k=1, min_cosine=-1)) == 1


async def test_a_chunk_found_by_both_searches_outranks_one_found_by_only_one(kb) -> None:
    hits = await ask(
        kb, "damaged electronics refund", top_k=8, pool=4, min_cosine=-1, min_ts_rank=0
    )
    both = [h for h in hits if h.cosine is not None and h.ts_rank is not None]
    single = [h for h in hits if (h.cosine is None) != (h.ts_rank is None)]
    assert both and single and min(h.rrf for h in both) > max(h.rrf for h in single)
    top = hits[0]
    # RRF = 1/(k + rank_vec) + 1/(k + rank_kw) for the best chunk: both ranks are small
    assert top.rrf > 1 / 61 and top.rrf <= 2 / 61


# --- the audience filter is in the SQL ------------------------------


async def internal_chunks(session) -> list[str]:
    return list(
        (
            await session.execute(
                text(
                    "SELECT c.heading || E'\\n' || c.content FROM kb_chunks c "
                    "JOIN kb_documents d ON d.id = c.document_id WHERE d.audience = 'internal'"
                )
            )
        ).scalars()
    )


@pytest.mark.parametrize("mode", ["hybrid", "vector", "keyword"])
async def test_a_customer_audience_never_returns_internal_chunks_in_any_mode(kb, mode) -> None:
    internal = await internal_chunks(kb)
    assert len(internal) >= 4
    queries = [
        *internal,  # the internal text itself: the best possible match
        "AMBER-HOLD RED-STOP escalation codes",
        "internal agent playbook for refund approvals",
        "fraud review signals and escalation path",
    ]
    for q in queries:
        hits = await ask(
            kb, q, audiences=CUSTOMER, mode=mode, top_k=6, min_cosine=-1, min_ts_rank=0
        )
        assert all(
            h.doc_title not in {d.title for d in DOCS if d.audience == "internal"} for h in hits
        ), q
        rows = await kb.execute(
            text("SELECT audience FROM kb_documents WHERE id = ANY(:ids)"),
            {"ids": [h.document_id for h in hits]},
        )
        assert set(rows.scalars()) <= {"customer"}, q


async def test_the_internal_documents_are_found_when_the_audience_list_includes_them(kb) -> None:
    hits = await ask(kb, "AMBER-HOLD RED-STOP escalation codes", audiences=BOTH)
    assert hits and hits[0].doc_title.startswith("Internal")


async def test_no_audience_means_no_documents_and_an_unknown_audience_is_an_error(kb) -> None:
    assert await ask(kb, "shipping", audiences=[]) == []
    with pytest.raises(ValueError):
        await ask(kb, "shipping", audiences=["public"])


async def test_the_audience_list_is_a_parameter_so_a_query_cannot_widen_it(kb) -> None:
    nasty = "x') OR audience = 'internal' -- AMBER-HOLD"
    hits = await ask(kb, nasty, audiences=CUSTOMER, min_cosine=-1, min_ts_rank=0)
    assert all(not h.doc_title.startswith("Internal") for h in hits)


# --- namespaces and models are isolated ------------------------------


async def test_another_namespace_is_a_separate_index(kb) -> None:
    await load(kb, ns="demo", docs=[d for d in DOCS if d.source == "shipping.md"])
    demo = await ask(kb, "how long does shipping take", ns="demo")
    assert demo and {h.doc_title for h in demo} == {"Shipping and delivery"}
    cancel = await ask(kb, "cancel an unpaid order", ns="demo", top_k=8)
    assert all(h.doc_title == "Shipping and delivery" for h in cancel)  # only what is in "demo"
    assert await ask(kb, "how long does shipping take", ns="nowhere") == []


async def test_chunks_made_by_another_embedding_model_are_never_compared(kb) -> None:
    other = "some-other-model"
    assert await ask(kb, "how long does shipping take", model=other, mode="vector") == []
    hybrid = await ask(kb, "how long does shipping take", model=other)
    assert all(h.cosine is None for h in hybrid)  # keywords do not depend on the model; vectors do
    await ensure_ready(kb, dim=768, namespace="default", model=EMB.model)
    with pytest.raises(KbNotIndexed, match=other):
        await ensure_ready(kb, dim=768, namespace="default", model=other)


# --- thresholds: "nothing relevant" ------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "who won the football world cup",
        "what is the capital of France",
        "how do I bake sourdough bread",
        "explain quantum entanglement",
        "best hiking trails in the alps",
    ],
)
async def test_out_of_scope_questions_return_nothing_at_the_default_thresholds(
    kb, question
) -> None:
    assert await ask(kb, question) == []


async def test_the_thresholds_decide_what_comes_back(kb) -> None:
    q = "shipping"
    assert await ask(kb, q, min_cosine=0.99, min_ts_rank=99) == []  # nothing is that relevant
    assert await ask(kb, q, min_cosine=-1, min_ts_rank=0, top_k=6)  # everything qualifies
    only_kw = await ask(kb, "shipping delivery", min_cosine=0.99, min_ts_rank=0.01)
    assert only_kw and all(h.cosine is None or h.cosine < 0.99 for h in only_kw)  # keyword rescue


# --- robustness ------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "return'; DROP TABLE kb_chunks; --",
        "a & b | !c :* ( ) <-> \\ ' \"",
        "   ",
        "the of and",
        "ünïcödé wörds 返品ポリシー",
        "x" * 300,
    ],
)
async def test_hostile_or_empty_queries_are_harmless(kb, q) -> None:
    await ask(kb, q, min_cosine=-1, min_ts_rank=0)  # no error
    assert await kb.scalar(text("SELECT count(*) FROM kb_chunks")) > 0  # nothing was dropped


def test_the_keyword_query_keeps_only_safe_alphanumeric_terms() -> None:
    assert keyword_query("Return policy for ELECTRONICS?!") == "return | policy | electronics"
    assert keyword_query("it's a-b ' | & ! :*") is None
    assert keyword_query("") is None and keyword_query("the of and") is None
    assert keyword_query("shipping shipping") == "shipping"
    assert keyword_query("x'); DROP TABLE a; --") == "drop | table"


async def test_a_ranking_is_the_same_after_a_re_ingest_with_new_random_ids(kb) -> None:
    """Ties (equal keyword rank or fused score) are broken by document and position, never by the
    random chunk id, so the order does not change between two ingests of the same text."""
    await load(kb, ns="again")
    for q in ("shipping", "order refund", "what happens to my order"):
        for mode in ("hybrid", "keyword", "vector"):
            a = await ask(kb, q, mode=mode, top_k=8, min_cosine=-1, min_ts_rank=0)
            b = await ask(kb, q, mode=mode, ns="again", top_k=8, min_cosine=-1, min_ts_rank=0)
            assert [(h.doc_title, h.heading) for h in a] == [(h.doc_title, h.heading) for h in b]
            assert {h.chunk_id for h in a}.isdisjoint({h.chunk_id for h in b})
