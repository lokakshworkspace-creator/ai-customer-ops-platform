"""Helpers for minting real and forged JWTs in tests."""

import uuid
import warnings
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import get_settings


def forge_token(
    sub: uuid.UUID | str | None,
    *,
    secret: str | None = None,
    algorithm: str = "HS256",
    expires_in: timedelta | None = timedelta(minutes=5),
    extra: dict[str, Any] | None = None,
    jti: str | None = "",
) -> str:
    """Build a JWT. `sub=None` / `expires_in=None` / `jti=None` omit that claim entirely; the
    default `jti` is a fresh unique one."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {"iat": now}
    if jti is not None:
        payload["jti"] = jti or uuid.uuid4().hex
    if sub is not None:
        payload["sub"] = str(sub)
    if expires_in is not None:
        payload["exp"] = now + expires_in
    payload.update(extra or {})
    key = None if algorithm == "none" else (secret or get_settings().jwt_secret)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # e.g. HS512 with a short key: fine for a forgery
        return jwt.encode(payload, key, algorithm=algorithm)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def auth_for(user_id: uuid.UUID) -> dict[str, str]:
    """A real login-issued token (not forged): goes through the real get_current_user."""
    from app.core.security import create_access_token

    return bearer(create_access_token(user_id)[0])
