"""Hybrid retrieval over the knowledge base: vector search + Postgres full-text search, merged with
Reciprocal Rank Fusion. A PURE module: no LLM, no HTTP, no prompt. It gets a query vector and a
query text and returns chunks.

SECURITY: **the audience filter is in the SQL**, in every candidate query and again in the final
select (`d.audience = ANY(:audiences)`). Who may see which documents is decided by the caller from
the authenticated user (never by the model, never by the prompt), and the database enforces it:
a chunk of an audience that is not in the list cannot be returned by any phrasing of the query.
The namespace and the embedding model are filtered the same way (vectors of different models are
not comparable).

Relevance: RRF only ranks, so "nothing relevant" is decided on ABSOLUTE scores: a chunk is returned
only if its cosine similarity is >= `min_cosine` or its keyword rank is >= `min_ts_rank`.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import KbAudience
from app.db.vector import vector_literal

Mode = Literal["hybrid", "vector", "keyword"]

_TERM = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    (
        "the and for are but not you your can how what when where which who why with from this "
        "that have has had was were will would should could does did any all our out about into "
        "than then them they their there its it's"
    ).split()
)

# The audience / namespace filter. Used in EVERY candidate query and the final select.
_SCOPE = "d.audience = ANY(:audiences) AND d.namespace = :ns"

_VEC_CTE = f"""
vec AS (
  SELECT c.id, 1 - (c.embedding <=> p.v) AS cosine,
         ROW_NUMBER() OVER (ORDER BY c.embedding <=> p.v) AS rnk
  FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id CROSS JOIN p
  WHERE {_SCOPE} AND c.embedding_model = :model
  ORDER BY c.embedding <=> p.v
  LIMIT :pool)"""
_KW_CTE = f"""
kw AS (
  SELECT c.id, ts_rank_cd(c.tsv, p.tsq, 32) AS ts_rank,
         ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.tsv, p.tsq, 32) DESC) AS rnk
  FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id CROSS JOIN p
  WHERE {_SCOPE} AND c.tsv @@ p.tsq
  ORDER BY ts_rank DESC
  LIMIT :pool)"""

_SQL = {
    "hybrid": f"""
WITH p AS (SELECT CAST(:qvec AS vector) AS v, to_tsquery('english', :tsq) AS tsq),
{_VEC_CTE},{_KW_CTE},
fused AS (
  SELECT COALESCE(vec.id, kw.id) AS id, vec.cosine, kw.ts_rank,
         COALESCE(1.0 / (:rrf_k + vec.rnk), 0) + COALESCE(1.0 / (:rrf_k + kw.rnk), 0) AS rrf
  FROM vec FULL OUTER JOIN kw ON kw.id = vec.id)
SELECT c.id, c.document_id, d.title, c.heading, c.content, f.cosine, f.ts_rank, f.rrf
FROM fused f JOIN kb_chunks c ON c.id = f.id JOIN kb_documents d ON d.id = c.document_id
WHERE {_SCOPE} AND (f.cosine >= :min_cosine OR f.ts_rank >= :min_ts_rank)
ORDER BY f.rrf DESC, c.id
LIMIT :top_k""",
    "vector": f"""
WITH p AS (SELECT CAST(:qvec AS vector) AS v),
{_VEC_CTE},
fused AS (SELECT vec.id, vec.cosine, NULL::real AS ts_rank,
                 1.0 / (:rrf_k + vec.rnk) AS rrf FROM vec)
SELECT c.id, c.document_id, d.title, c.heading, c.content, f.cosine, f.ts_rank, f.rrf
FROM fused f JOIN kb_chunks c ON c.id = f.id JOIN kb_documents d ON d.id = c.document_id
WHERE {_SCOPE} AND f.cosine >= :min_cosine
ORDER BY f.rrf DESC, c.id
LIMIT :top_k""",
    "keyword": f"""
WITH p AS (SELECT to_tsquery('english', :tsq) AS tsq),
{_KW_CTE},
fused AS (SELECT kw.id, NULL::double precision AS cosine, kw.ts_rank,
                 1.0 / (:rrf_k + kw.rnk) AS rrf FROM kw)
SELECT c.id, c.document_id, d.title, c.heading, c.content, f.cosine, f.ts_rank, f.rrf
FROM fused f JOIN kb_chunks c ON c.id = f.id JOIN kb_documents d ON d.id = c.document_id
WHERE {_SCOPE} AND f.ts_rank >= :min_ts_rank
ORDER BY f.rrf DESC, c.id
LIMIT :top_k""",
}


@dataclass(frozen=True, slots=True)
class RetrievalParams:
    top_k: int = 4
    pool: int = 20  # candidates taken from each of the two searches
    rrf_k: int = 60  # Reciprocal Rank Fusion constant
    min_cosine: float = 0.25
    min_ts_rank: float = 0.12


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    doc_title: str
    heading: str | None
    content: str
    cosine: float | None  # None when the chunk was found by keywords only
    ts_rank: float | None  # None when the chunk was found by vector similarity only
    rrf: float


def keyword_query(query: str) -> str | None:
    """An OR-ed tsquery from the plain alphanumeric words of the query (so it is injection-safe by
    construction: only [a-z0-9] survives, and the result is still passed as a parameter)."""
    terms: list[str] = []
    for t in _TERM.findall(query.lower()):
        if len(t) >= 3 and t not in _STOP and t not in terms:
            terms.append(t)
    return " | ".join(terms[:20]) or None


async def search(
    session: AsyncSession,
    *,
    query_vec: Sequence[float],
    query_text: str,
    audiences: Sequence[KbAudience | str],
    namespace: str,
    model: str,
    params: RetrievalParams | None = None,
    mode: Mode = "hybrid",
) -> list[Hit]:
    params = params or RetrievalParams()
    allowed = sorted({KbAudience(a).value for a in audiences})  # unknown audiences raise
    if not allowed:
        return []  # no audience, no documents
    tsq = keyword_query(query_text)
    if mode == "keyword" and tsq is None:
        return []
    bind: dict[str, object] = {
        "audiences": allowed,
        "ns": namespace,
        "model": model,
        "pool": params.pool,
        "top_k": params.top_k,
        "rrf_k": params.rrf_k,
        "min_cosine": params.min_cosine,
        "min_ts_rank": params.min_ts_rank,
        "qvec": vector_literal(query_vec),
        # a tsquery that matches nothing keeps the SQL valid when the query has no usable words
        "tsq": tsq or "zzzznomatchzzzz",
    }
    if mode == "keyword":
        del bind["qvec"], bind["model"], bind["min_cosine"]
    elif mode == "vector":
        del bind["tsq"], bind["min_ts_rank"]
    # with a filter, an HNSW scan can run out of matching rows; keep scanning until enough are found
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = (await session.execute(text(_SQL[mode]), bind)).all()
    return [
        Hit(r.id, r.document_id, r.title, r.heading, r.content, r.cosine, r.ts_rank, float(r.rrf))
        for r in rows
    ]


async def ensure_ready(session: AsyncSession, *, dim: int, namespace: str, model: str) -> None:
    """Fail loudly (rag/guards.py) if the configured dimension or model does not match the index."""
    from app.rag.guards import check_dimension, check_indexed

    await check_dimension(session, dim)
    await check_indexed(session, namespace, model)
