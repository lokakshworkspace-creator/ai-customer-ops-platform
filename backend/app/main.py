from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.deps import close_redis
from app.api.middleware import RequestIDMiddleware
from app.api.routers import approvals, auth, health, orders, refunds
from app.core.config import get_settings
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


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.is_prod)

    app = FastAPI(title="Ops Platform", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(orders.router)
    app.include_router(refunds.router)
    app.include_router(approvals.router)
    return app


app = create_app()
