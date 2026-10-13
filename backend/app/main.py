from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.deps import close_redis
from app.api.middleware import RequestIDMiddleware, SecurityHeadersMiddleware
from app.api.routers import admin, approvals, auth, chat, health, orders, refunds, workflows
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("startup", env=get_settings().app_env)
    yield
    await get_engine().dispose()
    await close_redis()
    log.info("shutdown")


def docs_urls(settings: Settings) -> dict[str, str | None]:
    """The interactive API docs and the schema are for development: with APP_ENV=prod there is no
    /docs, no /redoc and no /openapi.json (they would hand every visitor a map of the API)."""
    if settings.is_prod:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Ops Platform", version="0.1.0", lifespan=lifespan, **docs_urls(settings))
    # add_middleware puts the LAST one added outermost: the security headers wrap the request-id
    # middleware, so even the clean 500 it builds for a crash carries them.
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(orders.router)
    app.include_router(refunds.router)
    app.include_router(approvals.router)
    app.include_router(chat.router)
    app.include_router(workflows.router)
    app.include_router(admin.router)
    return app


configure_logging(get_settings().log_level, json_logs=get_settings().is_prod)
app = create_app()
