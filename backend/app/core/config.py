from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
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

    # HS256 is pinned in app.core.security (not configurable).
    jwt_secret: str = DEV_JWT_SECRET
    jwt_expire_minutes: int = 30

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
