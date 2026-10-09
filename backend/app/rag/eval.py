"""Retrieval evaluation against the golden questions (data/kb_eval/golden.yaml).

For every in-scope question and each search mode (vector, keyword, hybrid) the index is queried
with the relevance thresholds switched OFF, so the metric is pure RANKING quality:

* hit@1: the first chunk is from the expected document and holds the expected fragment;
* hit@3: one of the first three chunks is.

Separately, the hybrid search is run with the CONFIGURED thresholds to measure what the assistant
would really do: in-scope questions that get an answer, and out-of-scope questions that
correctly come back "not found". The best raw scores are reported too, because they are what the
thresholds are calibrated from.

The numbers belong to one embedder: nothing here says anything about another one. With the fake
embedder (lexical hashing) the numbers only show that the plumbing works.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import KbAudience
from app.rag.embedder import Embedder
from app.rag.kb import KB_DIR, KbDoc
from app.rag.retriever import RetrievalParams, search

GOLDEN_PATH = KB_DIR.parent / "kb_eval" / "golden.yaml"
MODES = ("vector", "keyword", "hybrid")
OPEN = RetrievalParams(top_k=3, min_cosine=-1.0, min_ts_rank=0.0)  # ranking only


@dataclass(frozen=True, slots=True)
class Golden:
    id: str
    question: str
    doc: str
    contains: str | None


@dataclass(slots=True)
class GoldenSet:
    questions: list[Golden]
    out_of_scope: list[str]


@dataclass(slots=True)
class ModeScore:
    hit1: int = 0
    hit3: int = 0
    misses: list[str] = field(default_factory=list)  # question ids not found in the top 3


@dataclass(slots=True)
class EvalReport:
    model: str
    namespace: str
    n: int
    modes: dict[str, ModeScore]
    answered_in_scope: int  # in-scope questions that returned something at the thresholds
    not_found_out_of_scope: int
    n_out: int
    in_scope_min_top_cosine: float | None  # worst best-cosine among in-scope questions
    out_scope_max_top_cosine: float | None  # best best-cosine among out-of-scope questions
    in_scope_min_top_ts: float
    out_scope_max_top_ts: float
    wrongly_answered: list[str] = field(default_factory=list)  # out-of-scope questions answered


def load_golden(path: Path = GOLDEN_PATH) -> GoldenSet:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    qs = [Golden(q["id"], q["q"], q["doc"], q.get("contains")) for q in raw["questions"]]
    return GoldenSet(qs, list(raw["out_of_scope"]))


def is_hit(g: Golden, hit_title: str, hit_text: str, titles: dict[str, str]) -> bool:
    """The chunk is from the expected document and (if given) holds the expected fragment."""
    if titles.get(hit_title) != g.doc:
        return False
    return g.contains is None or g.contains.lower() in hit_text.lower()


async def evaluate(
    session: AsyncSession,
    embedder: Embedder,
    docs: list[KbDoc],
    golden: GoldenSet,
    *,
    namespace: str,
    params: RetrievalParams,
) -> EvalReport:
    titles = {d.title: d.source for d in docs}
    audiences = [KbAudience.customer]
    modes = {m: ModeScore() for m in MODES}
    in_cos: list[float] = []
    in_ts: list[float] = []
    answered = 0

    async def run(text: str, mode: str, p: RetrievalParams) -> Any:
        vec = await embedder.embed_query(text) if mode != "keyword" else []
        return await search(
            session,
            query_vec=vec,
            query_text=text,
            audiences=audiences,
            namespace=namespace,
            model=embedder.model,
            params=p,
            mode=mode,  # type: ignore[arg-type]
        )

    async def best_scores(text: str) -> tuple[float | None, float]:
        """The best raw cosine and keyword rank for the question (thresholds off)."""
        vec_hits = await run(text, "vector", OPEN)
        kw_hits = await run(text, "keyword", OPEN)
        cos = max((h.cosine for h in vec_hits if h.cosine is not None), default=None)
        ts = max((h.ts_rank for h in kw_hits if h.ts_rank is not None), default=0.0)
        return cos, ts

    for g in golden.questions:
        for mode in MODES:
            hits = await run(g.question, mode, OPEN)
            ok = [is_hit(g, h.doc_title, f"{h.heading or ''}\n{h.content}", titles) for h in hits]
            if ok and ok[0]:
                modes[mode].hit1 += 1
            if any(ok):
                modes[mode].hit3 += 1
            else:
                modes[mode].misses.append(g.id)
        cos, ts = await best_scores(g.question)
        if cos is not None:
            in_cos.append(cos)
        in_ts.append(ts)
        if await run(g.question, "hybrid", params):
            answered += 1

    out_cos: list[float] = []
    out_ts: list[float] = []
    wrong: list[str] = []
    for q in golden.out_of_scope:
        cos, ts = await best_scores(q)
        if cos is not None:
            out_cos.append(cos)
        out_ts.append(ts)
        if await run(q, "hybrid", params):
            wrong.append(q)

    return EvalReport(
        model=embedder.model,
        namespace=namespace,
        n=len(golden.questions),
        modes=modes,
        answered_in_scope=answered,
        not_found_out_of_scope=len(golden.out_of_scope) - len(wrong),
        n_out=len(golden.out_of_scope),
        in_scope_min_top_cosine=min(in_cos, default=None),
        out_scope_max_top_cosine=max(out_cos, default=None),
        in_scope_min_top_ts=min(in_ts, default=0.0),
        out_scope_max_top_ts=max(out_ts, default=0.0),
        wrongly_answered=wrong,
    )


def format_report(r: EvalReport, params: RetrievalParams) -> str:
    def f(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.3f}"

    lines = [
        f"embedding model : {r.model}    namespace: {r.namespace}",
        f"questions       : {r.n} in scope, {r.n_out} out of scope",
        "",
        f"{'mode':<9}{'hit@1':>8}{'hit@3':>8}   missed in top 3",
    ]
    for mode in MODES:
        s = r.modes[mode]
        lines.append(f"{mode:<9}{s.hit1:>5}/{r.n}{s.hit3:>5}/{r.n}   {', '.join(s.misses) or '-'}")
    lines += [
        "",
        f"at the configured thresholds (min_cosine={params.min_cosine}, "
        f"min_ts_rank={params.min_ts_rank}):",
        f"  in-scope questions answered        : {r.answered_in_scope}/{r.n}",
        f"  out-of-scope correctly 'not found' : {r.not_found_out_of_scope}/{r.n_out}"
        + (f"   wrongly answered: {r.wrongly_answered}" if r.wrongly_answered else ""),
        "",
        "raw scores the thresholds are calibrated from:",
        f"  cosine  : lowest best-score of an in-scope question {f(r.in_scope_min_top_cosine)}, "
        f"highest best-score of an out-of-scope one {f(r.out_scope_max_top_cosine)}",
        f"  ts_rank : lowest {r.in_scope_min_top_ts:.3f} in scope, "
        f"highest {r.out_scope_max_top_ts:.3f} out of scope",
    ]
    return "\n".join(lines)
