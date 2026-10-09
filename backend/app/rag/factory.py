"""Which embedder the application uses, chosen by EMBEDDING_PROVIDER.

* `fake` (the DEFAULT): `FakeEmbedder`. Development and tests only; refused with any APP_ENV other
  than "dev" (the search then fails loudly instead of silently using meaningless vectors).
* `gemini`: `GeminiEmbedder`. Needs GEMINI_API_KEY and EMBEDDING_MODEL; a missing one is a clear
  configuration error, never a quiet fallback to the fake.
"""

from functools import lru_cache

import structlog

from app.core.config import Settings, get_settings
from app.rag.embedder import Embedder, EmbedderConfigError, FakeEmbedder, GeminiEmbedder
from app.rag.retriever import RetrievalParams

log = structlog.get_logger(__name__)


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "gemini":
        if settings.gemini_api_key is None:
            raise EmbedderConfigError("EMBEDDING_PROVIDER=gemini needs GEMINI_API_KEY.")
        if not settings.embedding_model:
            raise EmbedderConfigError(
                "EMBEDDING_PROVIDER=gemini needs EMBEDDING_MODEL (a Gemini embedding model name)."
            )
        return GeminiEmbedder(
            settings.gemini_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            timeout_s=settings.embedding_timeout_s,
            batch_size=settings.embedding_batch_size,
            min_interval_s=settings.embedding_min_interval_s,
            max_retries=settings.embedding_max_retries,
        )
    if settings.app_env == "dev":
        log.warning("using_fake_embedder", reason="EMBEDDING_PROVIDER is fake (the default)")
        return FakeEmbedder(settings.embedding_dim)
    raise EmbedderConfigError(
        "No embedder is configured: set EMBEDDING_PROVIDER=gemini with EMBEDDING_MODEL "
        "(and GEMINI_API_KEY)."
    )


def retrieval_params(embedder: Embedder) -> RetrievalParams:
    """Search parameters from settings; an unset threshold is the embedder's calibrated one."""
    s = get_settings()
    return RetrievalParams(
        top_k=s.rag_top_k,
        pool=s.rag_pool,
        rrf_k=s.rag_rrf_k,
        min_cosine=s.rag_min_cosine
        if s.rag_min_cosine is not None
        else embedder.default_min_cosine,
        min_ts_rank=(
            s.rag_min_ts_rank if s.rag_min_ts_rank is not None else embedder.default_min_ts_rank
        ),
    )


@lru_cache
def get_embedder() -> Embedder:
    """The process-wide embedder. API tests override the dependency that wraps this."""
    return build_embedder(get_settings())
