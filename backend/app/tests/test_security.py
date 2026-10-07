import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from argon2 import PasswordHasher

from app.core.config import get_settings
from app.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    password_needs_rehash,
)
from app.tests.tokens import forge_token

USER_ID = uuid.uuid4()


def test_token_roundtrip_and_expiry() -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    token, expires_in = create_access_token(USER_ID)
    assert expires_in == 30 * 60
    assert decode_access_token(token) == USER_ID

    claims = jwt.decode(token, options={"verify_signature": False})
    assert set(claims) == {"sub", "iat", "exp"}  # identity only: no role in the token
    exp = datetime.fromtimestamp(claims["exp"], UTC)
    assert before + timedelta(minutes=30) <= exp <= before + timedelta(minutes=30, seconds=5)
    assert jwt.get_unverified_header(token)["alg"] == "HS256"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("", id="empty"),
        pytest.param("not-a-jwt", id="garbage"),
        pytest.param("a.b.c", id="three-garbage-parts"),
        pytest.param(forge_token(USER_ID, expires_in=timedelta(seconds=-5)), id="expired"),
        pytest.param(forge_token(USER_ID, secret="z" * 40), id="wrong-secret"),
        pytest.param(forge_token(USER_ID, algorithm="none"), id="alg-none"),
        pytest.param(forge_token(USER_ID, algorithm="HS512"), id="wrong-hmac-alg"),
        pytest.param(forge_token(USER_ID, expires_in=None), id="no-exp"),
        pytest.param(forge_token(None), id="no-sub"),
        pytest.param(forge_token("not-a-uuid"), id="sub-not-a-uuid"),
        pytest.param(forge_token(USER_ID, extra={"sub": 12345}), id="sub-not-a-string"),
    ],
)
def test_bad_tokens_are_rejected(token: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_tampered_payload_is_rejected() -> None:
    token, _ = create_access_token(USER_ID)
    header, _payload, signature = token.split(".")
    other = forge_token(uuid.uuid4()).split(".")[1]
    with pytest.raises(InvalidTokenError):
        decode_access_token(f"{header}.{other}.{signature}")


def test_secret_comes_from_settings() -> None:
    token = jwt.encode(
        {"sub": str(USER_ID), "exp": datetime.now(UTC) + timedelta(minutes=1)},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    assert decode_access_token(token) == USER_ID


def test_needs_rehash_detects_weaker_parameters() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("pw")
    assert password_needs_rehash(weak)
    assert not password_needs_rehash(hash_password("pw"))
