"""Staff-only endpoints that are not tools.

GET /api/v1/admin/knowledge/search?q=...  searches the knowledge base INCLUDING the internal
documents, for administrators only.

* It calls the retriever directly: no LLM, no tool, and nothing the assistant can reach (the
  assistant's `search_knowledge_base` is hard-wired to customer documents for everyone).
* Authorization is `require_role(admin)`: a customer gets 403 before any embedding or search runs.
* The audiences come from the authenticated role in code (`rag.audience.audiences_for_role`), never
  from the request; the retriever enforces them inside the SQL.
* Every search writes an `audit_logs` row (who, how long the question was, what came back); the
  question's text is not stored.
* The embedding call (an HTTP request) happens with no database transaction open.
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_role
from app.core.config import get_settings
from app.db.models import User
from app.db.models.enums import Role
from app.modules import audit
from app.rag.audience import audiences_for_role
from app.rag.embedder import Embedder, EmbedderError
from app.rag.factory import get_embedder, retrieval_params
from app.rag.guards import KbConfigError
from app.rag.retriever import ensure_ready, search

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

UNAVAILABLE = {"detail": "The knowledge base is unavailable right now.", "code": "KB_UNAVAILABLE"}


def get_kb_embedder() -> Embedder:
    """The process-wide embedder (tests override this dependency)."""
    try:
        return get_embedder()
    except EmbedderError as exc:
        log.error("kb_embedder_unavailable", error=type(exc).__name__, detail=str(exc)[:200])
        raise HTTPException(503, detail=UNAVAILABLE["detail"]) from None


@router.get("/knowledge/search")
async def search_knowledge(
    request: Request,
    admin: Annotated[User, Depends(require_role(Role.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    embedder: Annotated[Embedder, Depends(get_kb_embedder)],
    q: Annotated[str, Query(min_length=2, max_length=500)],
) -> JSONResponse:
    # Plain data first, then end the transaction the login lookup opened: no transaction may be
    # open while we wait for the embedding API.
    actor_id, role = admin.id, admin.role
    request_id = getattr(request.state, "request_id", None)
    await session.rollback()
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(422, detail="The question is too short.")
    settings = get_settings()
    audiences = audiences_for_role(role)  # from the authenticated role, in code
    try:
        vector = await embedder.embed_query(query)
        await ensure_ready(
            session, dim=embedder.dim, namespace=settings.kb_namespace, model=embedder.model
        )
        hits = await search(
            session,
            query_vec=vector,
            query_text=query,
            audiences=audiences,
            namespace=settings.kb_namespace,
            model=embedder.model,
            params=retrieval_params(embedder),
        )
    except (EmbedderError, KbConfigError) as exc:
        log.error("kb_staff_search_failed", error=type(exc).__name__, detail=str(exc)[:300])
        await session.rollback()
        return JSONResponse(UNAVAILABLE, status_code=503)

    audit.record(
        session,
        actor_id=actor_id,
        action="admin.kb_search",
        resource_type="knowledge_base",
        resource_id=settings.kb_namespace,
        details={
            "query_chars": len(query),
            "results": len(hits),
            "internal_results": sum(1 for h in hits if h.audience == "internal"),
            "audiences": [a.value for a in audiences],
        },
        request_id=request_id,
    )
    await session.commit()
    return JSONResponse(
        {
            "found": bool(hits),
            "results": [
                {
                    "title": h.doc_title,
                    "audience": h.audience,
                    "heading": h.heading,
                    "content": h.content,
                    "score": round(h.cosine, 4) if h.cosine is not None else None,
                }
                for h in hits
            ],
        }
    )
