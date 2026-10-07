from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import get_settings
from app.db.session import get_session

__all__ = ["close_redis", "get_redis", "get_session"]


@lru_cache
def _redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


def get_redis() -> Redis:
    return _redis_client()


async def close_redis() -> None:
    await _redis_client().aclose()
