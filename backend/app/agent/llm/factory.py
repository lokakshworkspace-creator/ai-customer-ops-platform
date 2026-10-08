"""Which LLM the application uses, chosen by LLM_PROVIDER.

* `fake` (the DEFAULT): the gullible built-in `ObedientFakeLLM`. Development and tests only: with
  any APP_ENV other than "dev" it is refused, so the application never silently fakes its model
  somewhere that matters (the chat then answers 503).
* `gemini`: `GeminiLLMClient`. Needs GEMINI_API_KEY and LLM_MODEL; a missing one is a clear
  configuration error (503), never a quiet fallback to the fake.
"""

from functools import lru_cache

import structlog

from app.agent.llm.base import LLMClient, LLMConfigError
from app.agent.llm.fake import ObedientFakeLLM
from app.agent.llm.gemini import GeminiLLMClient
from app.core.config import Settings, get_settings

log = structlog.get_logger(__name__)


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "gemini":
        if settings.gemini_api_key is None:
            raise LLMConfigError("LLM_PROVIDER=gemini needs GEMINI_API_KEY.")
        if not settings.llm_model:
            raise LLMConfigError("LLM_PROVIDER=gemini needs LLM_MODEL (a Gemini model name).")
        return GeminiLLMClient(
            settings.gemini_api_key, model=settings.llm_model, timeout_s=settings.llm_timeout_s
        )
    if settings.app_env == "dev":
        log.warning("using_fake_llm", reason="LLM_PROVIDER is fake (the default)")
        return ObedientFakeLLM()
    raise LLMConfigError(
        "No LLM is configured: set LLM_PROVIDER=gemini with GEMINI_API_KEY and LLM_MODEL."
    )


@lru_cache
def get_llm_client() -> LLMClient:
    """The process-wide client. API tests override the FastAPI dependency that wraps this."""
    return build_llm_client(get_settings())
