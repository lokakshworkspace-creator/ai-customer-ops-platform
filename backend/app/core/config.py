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

    database_url: str = "postgresql+asyncpg://ops:ops@localhost:5432/ops"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9092"

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
