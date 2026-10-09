"""Knowledge-base ingest: plan (read-only), then apply (writes).

* **Unchanged is a no-op.** A document's `content_hash` covers its RENDERED text (so a changed
  rules.yaml value changes it), its title and audience, the embedding model and the dimension, and
  the chunking version. Same hash: nothing is embedded and nothing is written.
* **Changed replaces atomically.** One transaction per document: upsert the document row, delete its
  old chunks, insert the new ones. If embedding fails first (rate limit, outage) nothing is touched
  for that document and the old chunks keep serving; documents finished before the failure stay
  committed, so a re-run resumes where it stopped.
* **Cost control.** The embedder paces and retries its own calls (see rag/embedder.py); on top of
  that `max_calls` caps the number of API calls in one run so a bug cannot burn the quota.
* **Loud mismatches.** The embedder's dimension must equal the `kb_chunks.embedding` column
  (rag/guards.py) before anything is embedded.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.vector import vector_literal
from app.rag.chunker import Chunk, chunk_markdown
from app.rag.embedder import DocInput, Embedder
from app.rag.guards import check_dimension
from app.rag.kb import KbDoc

CHUNKING_VERSION = "1"  # bump when chunker.py changes how text is cut
Status = Literal["new", "changed", "unchanged"]


class IngestCapExceeded(Exception):
    """The run reached --max-calls. Finished documents are committed; re-run to continue."""


def content_hash(doc: KbDoc, model: str, dim: int) -> str:
    parts = [CHUNKING_VERSION, model, str(dim), doc.title, doc.audience.value, doc.body]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PlannedDoc:
    doc: KbDoc
    chunks: list[Chunk]
    hash: str
    status: Status
    version: int  # the version the row will have after applying


@dataclass(slots=True)
class IngestPlan:
    namespace: str
    model: str
    dim: int
    docs: list[PlannedDoc]
    orphans: list[str] = field(default_factory=list)  # in the database, no longer on disk

    @property
    def to_embed(self) -> list[PlannedDoc]:
        return [p for p in self.docs if p.status != "unchanged"]

    def estimated_calls(self, batch_size: int) -> int:
        return sum(-(-len(p.chunks) // batch_size) for p in self.to_embed)


@dataclass(slots=True)
class IngestResult:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    chunks_written: int = 0
    calls: int = 0


async def build_plan(
    session: AsyncSession, docs: list[KbDoc], *, namespace: str, model: str, dim: int
) -> IngestPlan:
    """Read-only: compare the documents on disk with what the database holds."""
    rows = (
        await session.execute(
            text("SELECT source, content_hash, version FROM kb_documents WHERE namespace = :ns"),
            {"ns": namespace},
        )
    ).all()
    existing = {r.source: r for r in rows}
    planned: list[PlannedDoc] = []
    for doc in docs:
        h = content_hash(doc, model, dim)
        row = existing.get(doc.source)
        if row is None:
            status, version = "new", 1
        elif row.content_hash == h:
            status, version = "unchanged", row.version
        else:
            status, version = "changed", row.version + 1
        planned.append(PlannedDoc(doc, chunk_markdown(doc.body), h, status, version))
    orphans = sorted(set(existing) - {d.source for d in docs})
    return IngestPlan(namespace, model, dim, planned, orphans)


_UPSERT = text(
    """
    INSERT INTO kb_documents (id, namespace, source, title, audience, version, content_hash,
                              embedding_model)
    VALUES (gen_random_uuid(), :ns, :source, :title, :audience, 1, :hash, :model)
    ON CONFLICT (namespace, source) DO UPDATE
       SET title = EXCLUDED.title, audience = EXCLUDED.audience,
           version = kb_documents.version + 1, content_hash = EXCLUDED.content_hash,
           embedding_model = EXCLUDED.embedding_model, updated_at = now()
    RETURNING id
    """
)
_INSERT_CHUNK = text(
    """
    INSERT INTO kb_chunks (id, document_id, ordinal, heading, content, embedding,
                           embedding_model, token_count)
    VALUES (gen_random_uuid(), :doc, :ordinal, :heading, :content, CAST(:vec AS vector),
            :model, :tokens)
    """
)


async def _replace_document(
    session: AsyncSession,
    planned: PlannedDoc,
    vectors: list[list[float]],
    *,
    namespace: str,
    model: str,
) -> None:
    """ONE transaction: upsert the row, delete the old chunks, insert the new ones."""
    doc = planned.doc
    doc_id = await session.scalar(
        _UPSERT,
        {
            "ns": namespace,
            "source": doc.source,
            "title": doc.title,
            "audience": doc.audience.value,
            "hash": planned.hash,
            "model": model,
        },
    )
    await session.execute(text("DELETE FROM kb_chunks WHERE document_id = :d"), {"d": doc_id})
    for chunk, vec in zip(planned.chunks, vectors, strict=True):
        await session.execute(
            _INSERT_CHUNK,
            {
                "doc": doc_id,
                "ordinal": chunk.ordinal,
                "heading": chunk.heading or None,
                "content": chunk.content,
                "vec": vector_literal(vec),
                "model": model,
                "tokens": chunk.token_count,
            },
        )


def embedding_inputs(planned: PlannedDoc) -> list[DocInput]:
    """What is embedded for each chunk: the document title, and the heading path plus the text."""
    return [
        DocInput(planned.doc.title, f"{c.heading}\n{c.content}" if c.heading else c.content)
        for c in planned.chunks
    ]


async def apply_plan(
    session: AsyncSession,
    plan: IngestPlan,
    embedder: Embedder,
    *,
    max_calls: int = 200,
    prune: bool = False,
) -> IngestResult:
    await check_dimension(session, embedder.dim)  # before ANY embedding call
    await session.rollback()  # end the read transaction: none is open during embedding calls
    result = IngestResult()
    calls_at_start = embedder.calls_made
    for planned in plan.docs:
        if planned.status == "unchanged":
            result.unchanged.append(planned.doc.source)
            continue
        if embedder.calls_made - calls_at_start >= max_calls:
            result.calls = embedder.calls_made - calls_at_start
            raise IngestCapExceeded(
                f"stopped after {result.calls} embedding calls (--max-calls {max_calls}); "
                f"{len(result.written)} document(s) written; run again to continue"
            )
        vectors = await embedder.embed_documents(embedding_inputs(planned))  # no txn open
        try:
            await _replace_document(
                session, planned, vectors, namespace=plan.namespace, model=plan.model
            )
            await session.commit()
        except Exception:
            await session.rollback()  # the old chunks keep serving
            raise
        result.written.append(planned.doc.source)
        result.chunks_written += len(planned.chunks)
    if prune and plan.orphans:
        await session.execute(
            text("DELETE FROM kb_documents WHERE namespace = :ns AND source = ANY(:s)"),
            {"ns": plan.namespace, "s": plan.orphans},
        )
        await session.commit()
        result.pruned = list(plan.orphans)
    result.calls = embedder.calls_made - calls_at_start
    return result
