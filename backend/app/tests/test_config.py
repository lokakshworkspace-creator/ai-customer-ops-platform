import pytest
from pydantic import ValidationError

from app.core.config import DEV_JWT_SECRET, Settings

STRONG = "x" * 32


def make(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)  # ignore the developer's real .env


def test_dev_allows_the_default_secret() -> None:
    s = make(app_env="dev")
    assert s.jwt_secret == DEV_JWT_SECRET
    assert len(DEV_JWT_SECRET) >= 32  # avoids PyJWT's weak-key warning in dev
    assert s.jwt_expire_minutes == 30


@pytest.mark.parametrize("env", ["prod", "test", "staging"])
def test_non_dev_refuses_the_default_secret(env: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        make(app_env=env)
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        make(app_env=env, jwt_secret=DEV_JWT_SECRET)


@pytest.mark.parametrize("secret", ["", "short", "x" * 31])
def test_non_dev_refuses_short_secrets(secret: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        make(app_env="prod", jwt_secret=secret)


def test_non_dev_accepts_a_strong_secret() -> None:
    assert make(app_env="prod", jwt_secret=STRONG).jwt_secret == STRONG
    assert make(app_env="prod", jwt_secret="y" * 64).is_prod
