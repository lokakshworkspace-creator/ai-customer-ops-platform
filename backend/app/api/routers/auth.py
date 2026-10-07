from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.core.security import create_access_token
from app.modules.users import service as users_service

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
    body: LoginRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> TokenResponse:
    user = await users_service.authenticate(session, body.email, body.password)
    if user is None:
        # Identical for unknown email, wrong password and inactive account.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )
    token, expires_in = create_access_token(user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)
