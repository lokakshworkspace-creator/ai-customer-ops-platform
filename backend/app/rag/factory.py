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


@lru_cache
def get_embedder() -> Embedder:
    """The process-wide embedder. API tests override the dependency that wraps this."""
    return build_embedder(get_settings())
