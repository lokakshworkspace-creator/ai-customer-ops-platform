"""search_knowledge_base: read-only lookup of shop policies and FAQs.

It runs through the SAME executor as every other tool (validate -> authorize -> policy -> execute ->
tool_calls log). Nothing about retrieval can authorize anything: the policy verdict is the ordinary
"read allowed", and the text it returns is DATA that informs an answer. rules.yaml and the
executor still decide every action.

* The audience is fixed here: the agent searches CUSTOMER documents only, whatever the caller's role
  (an administrator chatting with the assistant does not unlock internal documents). It is not an
  argument and the model cannot influence it.
* The embedding call (an HTTP request) and the search run in the after-commit hook, so no database
  transaction is held open while waiting for the embedding API.
* A knowledge-base configuration error (wrong dimension, nothing indexed for the current model) or
  an embedding outage fails the call with a generic error; the detail is in the tool_calls row and
  the log, never shown to the model.
"""

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.enums import KbAudience, ToolCallStatus
from app.policy import evaluate_read
from app.policy.rules import Rules
from app.rag.embedder import Embedder, EmbedderError
from app.rag.factory import get_embedder, retrieval_params
from app.rag.guards import KbConfigError
from app.rag.retriever import ensure_ready, search
from app.tools.context import ToolContext
from app.tools.handlers.base import AfterCommitResult, Decision, ToolResult
from app.tools.schemas import SearchKnowledgeBaseInput

log = structlog.get_logger(__name__)

# Fixed on purpose, for EVERY caller. Administrators read internal documents through the staff
# lookup (an admin-only endpoint that does not involve the model), never through the assistant.
AGENT_AUDIENCES: tuple[KbAudience, ...] = (KbAudience.customer,)


class SearchKnowledgeBaseHandler:
    def __init__(self, embedder: Embedder | None = None) -> None:
        self._embedder = embedder  # None: resolved on first use (EMBEDDING_PROVIDER)

    async def decide(
        self, session: AsyncSession, ctx: ToolContext, args: SearchKnowledgeBaseInput, rules: Rules
    ) -> Decision:
        return Decision(evaluate_read(rules, ctx.actor))

    async def execute(
        self,
        session: AsyncSession,
        ctx: ToolContext,
        args: SearchKnowledgeBaseInput,
        decision: Decision,
    ) -> ToolResult:
        async def hook(after: AsyncSession) -> AfterCommitResult:
            return await self._search(after, args.query)

        return ToolResult(data={"query": args.query}, after_commit=hook)

    async def _search(self, session: AsyncSession, query: str) -> AfterCommitResult:
        s = get_settings()
        try:
            embedder = self._embedder or get_embedder()
            vec = await embedder.embed_query(query)  # HTTP, with no transaction open
            await ensure_ready(
                session, dim=embedder.dim, namespace=s.kb_namespace, model=embedder.model
            )
            hits = await search(
                session,
                query_vec=vec,
                query_text=query,
                audiences=AGENT_AUDIENCES,
                namespace=s.kb_namespace,
                model=embedder.model,
                params=retrieval_params(embedder),
            )
        except (EmbedderError, KbConfigError) as exc:
            log.error("kb_search_failed", error=type(exc).__name__, detail=str(exc)[:300])
            await session.rollback()
            return AfterCommitResult(
                status=ToolCallStatus.failed,
                error_code="KB_UNAVAILABLE",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                http_status=503,
            )
        results: list[dict[str, Any]] = [
            {
                "document_id": str(h.document_id),
                "chunk_id": str(h.chunk_id),
                "title": h.doc_title,
                "heading": h.heading,
                "content": h.content,
                "score": round(h.cosine, 4) if h.cosine is not None else None,
            }
            for h in hits
        ]
        return AfterCommitResult(
            status=ToolCallStatus.succeeded, data={"found": bool(results), "results": results}
        )
