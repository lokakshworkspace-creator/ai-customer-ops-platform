import asyncio
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_redis, get_session

router = APIRouter(tags=["health"])
log = structlog.get_logger(__name__)

CHECK_TIMEOUT_S = 2.0

CheckStatus = Literal["ok", "error"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    postgres: CheckStatus
    redis: CheckStatus


async def _check_postgres(session: AsyncSession) -> CheckStatus:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        log.warning("health_check_failed", dependency="postgres", exc_info=True)
        return "error"


async def _check_redis(redis: Redis) -> CheckStatus:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            await redis.ping()
        return "ok"
    except Exception:
        log.warning("health_check_failed", dependency="redis", exc_info=True)
        return "error"


@router.get("/health", response_model=HealthResponse)
async def health(
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> HealthResponse:
    pg, rd = await asyncio.gather(_check_postgres(session), _check_redis(redis))
    healthy = pg == "ok" and rd == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="ok" if healthy else "degraded", postgres=pg, redis=rd)
