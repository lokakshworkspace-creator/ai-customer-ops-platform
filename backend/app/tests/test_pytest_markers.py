"""The `kafka` marker: registered, strict, and applied to every test that uses `kafka_env`."""

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
TESTS = BACKEND / "app" / "tests"


def collect(expression: str) -> set[str]:
    """Test ids (parametrisation stripped) that `pytest -m EXPRESSION` would run."""
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            expression,
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stdout[-800:] + out.stderr[-800:]
    return {re.sub(r"\[.*\]$", "", ln.strip()) for ln in out.stdout.splitlines() if "::" in ln}


def scan_for_kafka_env_users() -> set[str]:
    """Every test function that asks for the `kafka_env` fixture directly, found by reading the
    source (independently of the hook that is under test)."""
    found: set[str] = set()
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test_"
            ):
                args = [a.arg for a in node.args.args + node.args.kwonlyargs]
                if "kafka_env" in args:
                    found.add(f"app/tests/{path.name}::{node.name}")
    return found


def test_the_marker_is_registered_and_markers_are_strict() -> None:
    cfg = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    ini = cfg["tool"]["pytest"]["ini_options"]
    assert "--strict-markers" in ini["addopts"]
    assert any(m.startswith("kafka:") for m in ini["markers"])


def test_every_test_using_kafka_env_is_marked_and_nothing_else_leaks_into_the_fast_suite() -> None:
    needs_kafka = scan_for_kafka_env_users()
    assert len(needs_kafka) >= 25, "the scan found suspiciously few Redpanda tests"

    marked = collect("kafka")
    fast = collect("not kafka")
    missing = needs_kafka - marked
    assert not missing, f"tests using kafka_env but NOT marked kafka: {sorted(missing)}"
    leaked = needs_kafka & fast
    assert not leaked, f"Redpanda tests selected by -m 'not kafka': {sorted(leaked)}"
    assert not marked & fast, "a test is in both selections"
    # the two selections together are the whole suite
    assert len(marked) > 0 and len(fast) > 0


def test_an_unregistered_marker_is_an_error() -> None:
    probe = TESTS / "_probe_unregistered_marker.py"
    probe.write_text(
        "import pytest\n\n@pytest.mark.not_a_registered_marker\ndef test_x():\n    pass\n",
        encoding="utf-8",
    )
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(probe)],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        probe.unlink()
    assert out.returncode != 0 and "not_a_registered_marker" in (out.stdout + out.stderr)
