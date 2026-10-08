"""Throw-away-data scripts (the demo, the demo cleanup) refuse to run outside APP_ENV=dev, and they
refuse BEFORE touching settings validation or any database."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import config
from app.core.config import configured_app_env, dev_only_refusal

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"
BACKEND = REPO / "backend"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- configured_app_env: the raw value, without validating Settings -----------------------------


def test_the_environment_variable_wins(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=prod\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("APP_ENV", "test")
    assert configured_app_env() == "test"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("APP_ENV=prod", "prod"),
        ("APP_ENV = staging", "staging"),
        ('APP_ENV="prod"', "prod"),
        ("APP_ENV='prod'", "prod"),
        ("APP_ENV=dev                # dev | test | prod", "dev"),
        ("app_env=prod  # lower case key", "prod"),
        ("  APP_ENV=prod  ", "prod"),
    ],
)
def test_it_reads_the_dotenv_file_like_the_settings_do(
    monkeypatch, tmp_path, line, expected
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"LOG_LEVEL=INFO\n{line}\nJWT_EXPIRE_MINUTES=30\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.delenv("APP_ENV", raising=False)
    assert configured_app_env() == expected


def test_it_defaults_to_dev(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.delenv("APP_ENV", raising=False)
    assert configured_app_env() == "dev"
    (tmp_path / ".env").write_text("LOG_LEVEL=INFO\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    assert configured_app_env() == "dev"


def test_it_agrees_with_what_settings_would_read(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    assert configured_app_env() == config.Settings().app_env


# --- dev_only_refusal ---------------------------------------------------------------------------


def test_dev_is_allowed_and_everything_else_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    assert dev_only_refusal("tool.py") is None
    for env in ("test", "prod", "staging", "production", "Dev", ""):
        monkeypatch.setenv("APP_ENV", env)
        msg = dev_only_refusal("tool.py")
        if env == "":  # an empty value means "unset" -> the default, dev
            assert msg is None
        else:
            assert msg and "tool.py refuses to run" in msg and repr(env) in msg
            assert "APP_ENV=dev" in msg and "Nothing was touched" in msg


# --- the demo -----------------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["test", "prod", "staging"])
async def test_the_demo_refuses_outside_dev_before_touching_anything(
    monkeypatch, capsys, env: str
) -> None:
    demo = load("demo_phase2")

    def touched(*a, **kw):
        raise AssertionError("the demo touched settings or the database before refusing")

    for name in ("get_settings", "preflight", "create_demo_data", "scalar", "rows", "counts"):
        monkeypatch.setattr(demo, name, touched)
    monkeypatch.setenv("APP_ENV", env)

    assert await demo.main(8000) == 2
    err = capsys.readouterr().err
    assert f"demo_phase2.py refuses to run with APP_ENV={env!r}" in err
    assert "Nothing was touched" in err


def test_the_demo_as_a_real_process_exits_2_with_a_clear_message_and_no_traceback() -> None:
    """With APP_ENV=prod and the default JWT secret, building Settings would raise a validation
    error. The refusal must come first. DATABASE_URL points at a closed port, so touching the
    database would produce a different (connection) error."""
    env = {
        **os.environ,
        "APP_ENV": "prod",
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": "postgresql+asyncpg://x:x@127.0.0.1:1/x",
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "demo_phase2.py")],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,  # a cold Python start alone can take ~11 s
    )
    assert done.returncode == 2
    assert "demo_phase2.py refuses to run with APP_ENV='prod'" in done.stderr
    assert "Traceback" not in done.stderr and "ValidationError" not in done.stderr
    assert "cannot reach" not in done.stderr


# --- the phase 3 demo ----------------------------------------------------------------------


@pytest.mark.parametrize("env", ["test", "prod", "staging"])
@pytest.mark.parametrize("prepare_only", [False, True])
async def test_the_chat_demo_refuses_outside_dev_before_touching_anything(
    monkeypatch, capsys, env: str, prepare_only: bool
) -> None:
    demo = load("demo_phase3")

    def touched(*a, **kw):
        raise AssertionError("the demo touched settings or the database before refusing")

    monkeypatch.setattr(demo, "get_settings", touched)
    monkeypatch.setattr(demo, "preflight", touched)
    monkeypatch.setattr(demo.d2, "create_demo_data", touched)
    monkeypatch.setenv("APP_ENV", env)

    assert await demo.main(8000, prepare_only) == 2
    err = capsys.readouterr().err
    assert f"demo_phase3.py refuses to run with APP_ENV={env!r}" in err


def test_the_chat_demo_as_a_real_process_exits_2_outside_dev() -> None:
    env = {
        **os.environ,
        "APP_ENV": "prod",
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": "postgresql+asyncpg://x:x@127.0.0.1:1/x",
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "demo_phase3.py")],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,  # a cold Python start alone can take ~11 s
    )
    assert done.returncode == 2
    assert "demo_phase3.py refuses to run with APP_ENV='prod'" in done.stderr
    assert "Traceback" not in done.stderr
