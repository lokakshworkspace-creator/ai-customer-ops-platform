import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repo root, one level above backend/
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


DEV_JWT_SECRET = "dev-only-insecure-secret-do-not-use-outside-dev"
MIN_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",  # .env is shared with docker-compose (POSTGRES_* etc.)
    )

    app_env: str = "dev"  # dev | test | prod
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://ops:ops@127.0.0.1:5432/ops"
    redis_url: str = "redis://127.0.0.1:6379/0"
    kafka_bootstrap_servers: str = "127.0.0.1:9092"

    # Business rules file; empty means the packaged app/policy/rules.yaml.
    policy_rules_path: str | None = None

    # Simulated payment provider (development / tests).
    payment_sim_latency_ms: int = 0
    payment_sim_failure_rate: float = 0.0  # probability in [0, 1]

    # Event pipeline (outbox relay and consumers).
    kafka_topic_prefix: str = "ops"
    kafka_request_timeout_ms: int = 10000
    kafka_startup_timeout_s: float = 10.0
    outbox_poll_interval_s: float = 1.0
    outbox_batch_size: int = 50
    # After this many failed publish attempts an event is PARKED (retries stop, and later events
    # of the same aggregate are blocked until someone un-parks it: scripts/outbox_admin.py).
    outbox_max_attempts: int = 10
    outbox_backoff_base_s: float = 1.0  # delay after the 1st failure; doubles each time
    outbox_backoff_max_s: float = 300.0
    # `relay --once` / `consumer --once` give up after this many seconds (0 = no limit) and exit 3
    # naming what they were waiting on, instead of hanging.
    once_deadline_s: float = 120.0
    consumer_group: str = "notifications"
    consumer_poll_timeout_ms: int = 1000
    consumer_max_handler_attempts: int = 5
    consumer_handler_backoff_s: float = 0.5

    # `scripts/reconcile_refunds.py` retries refunds stuck in `processing` for longer than this.
    refund_stuck_after_seconds: int = 300

    # The agent (Phase 3). `fake` (the default) is a built-in rule-based model for dev and tests;
    # `gemini` calls the Google Gemini REST API and needs GEMINI_API_KEY and LLM_MODEL (a model
    # name such as one of Google's "flash-lite" models: deliberately not hard-coded here).
    # The key is a SecretStr: it prints as ********** and must never be logged or committed.
    llm_provider: Literal["fake", "gemini"] = "fake"
    gemini_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_s: float = Field(30.0, gt=0)
    llm_max_tokens: int = Field(1024, ge=1)  # sent on EVERY LLM call
    llm_history_messages: int = Field(20, ge=1)  # at most this many messages are sent to the model
    agent_max_tool_calls_per_turn: int = Field(5, ge=1)
    agent_max_tool_calls_per_conversation: int = Field(20, ge=1)
    agent_turn_deadline_s: float = Field(90.0, gt=0)  # one chat turn, all LLM and tool calls
    agent_turns_per_hour: int = Field(30, ge=1)  # per user: each turn costs real LLM money
    agent_max_message_chars: int = Field(2000, ge=1)

    # HS256 is pinned in app.core.security (not configurable).
    jwt_secret: str = DEV_JWT_SECRET
    jwt_expire_minutes: int = 30

    @field_validator("gemini_api_key", "llm_model", mode="before")
    @classmethod
    def _blank_means_unset(cls, v: object) -> object:
        """`GEMINI_API_KEY=` / `LLM_MODEL=` (blank, as in .env.example) mean "not set"."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _require_strong_jwt_secret_outside_dev(self) -> "Settings":
        if self.app_env != "dev" and (
            self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < MIN_JWT_SECRET_LENGTH
        ):
            raise ValueError(
                f"JWT_SECRET must be set to a unique value of at least {MIN_JWT_SECRET_LENGTH} "
                f"characters when APP_ENV is not 'dev' (APP_ENV={self.app_env!r})"
            )
        return self

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def configured_app_env() -> str:
    """APP_ENV as the application would see it (environment variable, else `.env`, else "dev"),
    read WITHOUT building `Settings`. Building Settings validates everything, and a non-dev
    environment with a weak JWT secret fails validation, so a script that wants to refuse to run
    outside development must look at the raw value first and say so clearly."""
    value = os.environ.get("APP_ENV")
    if value is None and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, rest = line.strip().partition("=")
            if sep and key.strip().upper() == "APP_ENV":
                value = re.split(r"\s+#", rest.strip())[0].strip().strip("\"'")
                break
    return (value or "dev").strip()


def dev_only_refusal(tool: str) -> str | None:
    """A message explaining why `tool` must not run, or None when APP_ENV is "dev"."""
    env = configured_app_env()
    if env == "dev":
        return None
    return (
        f"{tool} refuses to run with APP_ENV={env!r}: it creates or deletes throw-away data and "
        "is for local development only (APP_ENV=dev). Nothing was touched."
    )
