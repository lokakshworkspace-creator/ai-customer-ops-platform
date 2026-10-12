import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings

# argon2id with the library's current recommended parameters.
_hasher = PasswordHasher()

# Pinned: never read from the token header and not configurable.
JWT_ALGORITHM = "HS256"


# --- passwords ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True if the stored hash used weaker parameters than the current policy."""
    return _hasher.check_needs_rehash(password_hash)


@lru_cache
def dummy_password_hash() -> str:
    """A valid hash of a throwaway value. Verifying against it when the user doesn't exist
    makes unknown-email logins cost the same as wrong-password logins."""
    return hash_password("dummy-password-for-timing-equalisation")


# --- JWT ---------------------------------------------------------------------------------


class InvalidTokenError(Exception):
    """Token is malformed, expired, badly signed, uses the wrong algorithm, or lacks claims."""


def create_access_token(user_id: uuid.UUID, *, now: datetime | None = None) -> tuple[str, int]:
    """Return (token, expires_in_seconds).

    The token carries identity only (`sub`) and its own unique id (`jti`, so that logout can
    revoke exactly this token). Authorisation data such as the role is deliberately absent: it is
    always read from the database on each request.
    """
    settings = get_settings()
    issued = now or datetime.now(UTC)
    lifetime = timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": str(user_id),
        "jti": uuid.uuid4().hex,
        "iat": issued,
        "exp": issued + lifetime,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)
    return token, int(lifetime.total_seconds())


@dataclass(frozen=True)
class TokenClaims:
    user_id: uuid.UUID
    jti: str
    expires_at: datetime


def decode_token(token: str) -> TokenClaims:
    """Validate the token (signature, algorithm, expiry, required claims) and return its claims.
    `jti` is required: a token without one cannot be revoked, so it is not accepted. This does NOT
    check revocation (that needs the database: see `get_current_user`). Raises InvalidTokenError."""
    try:
        claims = jwt.decode(
            token,
            get_settings().jwt_secret,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "sub", "jti"]},
        )
        jti = str(claims["jti"])
        if not jti or len(jti) > 64:
            raise ValueError("bad jti")
        return TokenClaims(
            user_id=uuid.UUID(str(claims["sub"])),
            jti=jti,
            expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
        )
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        raise InvalidTokenError(type(exc).__name__) from exc


def decode_access_token(token: str) -> uuid.UUID:
    """Validate the token and return the user id from `sub`. Raises InvalidTokenError."""
    return decode_token(token).user_id
