"""The golden questions and the eval harness. With the FAKE embedder these tests prove plumbing
only (the files are well-formed, the metrics are computed, the out-of-scope questions come back
'not found'). They say nothing about how good real retrieval is, and nothing here compares the
search modes: that is reported only from the real-embedder run."""

import pytest

from app.policy.rules import get_rules
from app.rag.embedder import FakeEmbedder
from app.rag.eval import MODES, Golden, evaluate, format_report, is_hit, load_golden
from app.rag.kb import load_documents
from app.rag.retriever import RetrievalParams
from app.tools.handlers.knowledge import retrieval_params

DOCS = load_documents(get_rules())
BY_SOURCE = {d.source: d for d in DOCS}
GOLDEN = load_golden()
EMB = FakeEmbedder(768)


def test_the_golden_set_has_25_in_scope_and_5_out_of_scope_questions() -> None:
    assert len(GOLDEN.questions) == 25 and len(GOLDEN.out_of_scope) == 5
    ids = [g.id for g in GOLDEN.questions]
    assert len(ids) == len(set(ids))
    questions = [g.question for g in GOLDEN.questions] + GOLDEN.out_of_scope
    assert len(questions) == len(set(questions))


@pytest.mark.parametrize("g", GOLDEN.questions, ids=lambda g: g.id)
def test_every_golden_question_points_at_a_customer_document_that_holds_the_fragment(g) -> None:
    doc = BY_SOURCE[g.doc]
    assert doc.audience == "customer", "a golden question must be answerable from customer text"
    assert g.contains and g.contains.lower() in doc.body.lower()


def test_every_customer_document_is_the_target_of_at_least_one_question() -> None:
    targets = {g.doc for g in GOLDEN.questions}
    assert {d.source for d in DOCS if d.audience == "customer"} <= targets


def test_a_hit_needs_the_right_document_and_the_right_fragment() -> None:
    g = Golden("x", "q", "shipping.md", "business days")
    titles = {d.title: d.source for d in DOCS}
    assert is_hit(g, "Shipping and delivery", "Takes 3 to 5 Business Days.", titles)
    assert not is_hit(g, "Shipping and delivery", "Free shipping.", titles)  # wrong fragment
    assert not is_hit(g, "Refunds", "3 to 5 business days", titles)  # wrong document
    assert is_hit(Golden("y", "q", "shipping.md", None), "Shipping and delivery", "", titles)


async def test_the_eval_runs_end_to_end_and_the_out_of_scope_questions_are_not_found(
    db_session,
) -> None:
    from app.rag.ingest import apply_plan, build_plan

    plan = await build_plan(db_session, DOCS, namespace="eval", model=EMB.model, dim=EMB.dim)
    await apply_plan(db_session, plan, EMB)
    params = retrieval_params(EMB)
    report = await evaluate(db_session, EMB, DOCS, GOLDEN, namespace="eval", params=params)

    assert report.n == 25 and report.n_out == 5 and report.model == EMB.model
    for mode in MODES:
        s = report.modes[mode]
        assert 0 <= s.hit1 <= s.hit3 <= report.n  # a top-1 hit is also a top-3 hit
        assert len(s.misses) == report.n - s.hit3
    assert report.not_found_out_of_scope == 5 and report.wrongly_answered == []
    assert 0 <= report.answered_in_scope <= 25
    assert report.out_scope_max_top_cosine is not None
    text = format_report(report, params)
    assert "hit@1" in text and "hybrid" in text and "FAKE" not in text  # (the script adds that)


async def test_the_eval_only_reads_the_index_it_is_pointed_at(db_session) -> None:
    report = await evaluate(
        db_session, EMB, DOCS, GOLDEN, namespace="nothing-here", params=RetrievalParams()
    )
    assert all(s.hit3 == 0 for s in report.modes.values()) and report.answered_in_scope == 0
