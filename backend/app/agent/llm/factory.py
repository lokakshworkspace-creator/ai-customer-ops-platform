"""Which LLM the application uses.

* An `ANTHROPIC_API_KEY` is configured -> `ClaudeLLMClient`.
* No key and `APP_ENV=dev` -> the gullible `ObedientFakeLLM` (offline development and the demo).
* No key anywhere else -> `LLMConfigError`: the chat answers 503. The application never silently
  fakes its model outside development.
"""

from functools import lru_cache

import structlog

from app.agent.llm.base import LLMClient, LLMConfigError
from app.agent.llm.claude import ClaudeLLMClient
from app.agent.llm.fake import ObedientFakeLLM
from app.core.config import Settings, get_settings

log = structlog.get_logger(__name__)


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.anthropic_api_key is not None:
        return ClaudeLLMClient(
            settings.anthropic_api_key,
            model=settings.anthropic_model,
            timeout_s=settings.llm_timeout_s,
        )
    if settings.app_env == "dev":
        log.warning("using_fake_llm", reason="ANTHROPIC_API_KEY is not set")
        return ObedientFakeLLM()
    raise LLMConfigError("No LLM is configured: set ANTHROPIC_API_KEY.")


@lru_cache
def get_llm_client() -> LLMClient:
    """The process-wide client. API tests override the FastAPI dependency that wraps this."""
    return build_llm_client(get_settings())
