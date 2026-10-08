from collections.abc import Callable, Coroutine
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm.base import LLMClient, LLMConfigError
from app.agent.llm.factory import get_llm_client
from app.agent.orchestrator import Orchestrator
from app.core.config import get_settings
from app.core.security import InvalidTokenError, decode_access_token
from app.db.models import User
from app.db.models.enums import Role
from app.db.session import get_session
from app.modules.users import repo as users_repo
from app.policy.rules import get_rules
from app.tools.default import build_default_registry
from app.tools.executor import ToolExecutor

__all__ = [
    "CurrentUser",
    "get_executor",
    "get_llm",
    "get_orchestrator",
    "close_redis",
    "get_current_user",
    "get_redis",
    "get_session",
    "require_role",
]


@lru_cache
def _redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


def get_redis() -> Redis:
    return _redis_client()


async def close_redis() -> None:
    await _redis_client().aclose()


_bearer = HTTPBearer(auto_error=False)


def _unauthenticated() -> HTTPException:
    # One response for every kind of failure: missing, malformed, expired, bad signature,
    # unknown user, inactive user. Nothing here helps an attacker tell them apart.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Authenticate the request from its bearer token.

    The token only proves *who* the caller is. The user is reloaded from the database on
    every request, so deactivation, deletion and role changes take effect immediately, and
    the role used for authorisation always comes from the database, never from the token.
    """
    if credentials is None:
        raise _unauthenticated()
    try:
        user_id = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise _unauthenticated() from None

    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.is_active:
        raise _unauthenticated()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: Role) -> Callable[..., Coroutine[Any, Any, User]]:
    """Dependency factory: allow only users whose (database) role is one of `roles`."""
    allowed = frozenset(roles)

    async def checker(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
            )
        return user

    return checker


@lru_cache
def _executor() -> ToolExecutor:
    return ToolExecutor(build_default_registry(get_rules()))


def get_executor() -> ToolExecutor:
    """The process-wide tool executor (registry + payment provider). Tests override this."""
    return _executor()


def get_llm() -> LLMClient:
    """The process-wide LLM client (Claude with a key, the fake in dev without one). Without a
    key outside dev there is no model: answer 503 rather than pretending."""
    try:
        return get_llm_client()
    except LLMConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None


def get_orchestrator(
    executor: Annotated[ToolExecutor, Depends(get_executor)],
    llm: Annotated[LLMClient, Depends(get_llm)],
) -> Orchestrator:
    return Orchestrator(executor, llm, get_settings())
