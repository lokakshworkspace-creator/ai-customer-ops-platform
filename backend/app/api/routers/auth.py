from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_login_limiter, get_session
from app.core.config import get_settings
from app.core.login_limiter import LoginLimiter, client_ip
from app.core.security import create_access_token
from app.modules.users import service as users_service

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # Plain str on purpose: login must never reveal anything about email validity rules.
    email: Annotated[str, Field(min_length=1, max_length=254)]
    # Upper bound stops someone making us run argon2 over megabytes of input.
    password: Annotated[str, Field(min_length=1, max_length=1024)]


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limiter: Annotated[LoginLimiter, Depends(get_login_limiter)],
) -> TokenResponse:
    ip = client_ip(request, get_settings().trusted_proxy_hops)
    # Before any password work: a blocked caller learns nothing and costs us no argon2.
    wait = await limiter.retry_after(body.email, ip)
    if wait is not None:
        log.warning("login_blocked", key=limiter.key(body.email, ip), retry_after=wait)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    user = await users_service.authenticate(session, body.email, body.password)
    if user is None:
        await limiter.record_failure(body.email, ip)
        # Identical for unknown email, wrong password and inactive account.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )
    await limiter.reset(body.email, ip)
    token, expires_in = create_access_token(user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me")
async def me(user: CurrentUser) -> dict[str, str]:
    """Who the caller is, read from the DATABASE (never from the token's claims). Used by the web
    page to choose what to show; it is not an authority: every endpoint authorizes for itself."""
    return {"id": str(user.id), "email": user.email, "role": user.role.value}
