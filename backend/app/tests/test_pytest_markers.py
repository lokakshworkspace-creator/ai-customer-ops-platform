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


def test_a_default_per_test_timeout_is_configured(pytestconfig) -> None:
    """pytest-timeout guards the suite against a test that hangs (see the README)."""
    cfg = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["tool"]["pytest"]["ini_options"]["timeout"] == 180
    assert any("pytest-timeout" in dep for dep in cfg["project"]["optional-dependencies"]["dev"])
    assert float(pytestconfig.getini("timeout")) == 180  # and pytest really reads it


def test_a_hanging_test_is_stopped_by_the_timeout_instead_of_hanging(tmp_path) -> None:
    """What pytest-timeout does depends on the platform. On Windows it can only use the `thread`
    method: it prints the stack of every thread and ends the WHOLE pytest process (exit code 1)
    at the hanging test. That test is not reported as FAILED, there is no summary line, and the
    tests after it never run. (On POSIX the `signal` method fails only the hanging test and the
    run continues.) The README says exactly this, so this test pins it."""
    (tmp_path / "test_a_before.py").write_text("def test_before():\n    pass\n", encoding="utf-8")
    (tmp_path / "test_b_hang.py").write_text(
        "import time\n\n\ndef test_hangs():\n    time.sleep(120)\n", encoding="utf-8"
    )
    (tmp_path / "test_c_after.py").write_text("def test_after():\n    pass\n", encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            "timeout=3",
            "-v",
            "--rootdir",
            str(tmp_path),
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=100,  # far below the 120 s the test would sleep; cold start is ~11 s
    )
    out = done.stdout + done.stderr
    assert done.returncode != 0
    assert "test_before PASSED" in out  # tests before the hang ran
    assert "Timeout" in out and "time.sleep(120)" in out  # the stack dump shows where it hung
    if sys.platform == "win32":
        assert "timeout method: thread" in out
        assert "test_after" not in out  # the whole run was ended; later tests never ran
        assert "FAILED" not in out and " passed" not in out  # no per-test result, no summary
    else:
        assert "test_after PASSED" in out  # the signal method fails only the hanging test
