"""The mutation lists in scripts/mutation/ must stay in step with the code they target.

This does not run any mutation (that takes minutes; see scripts/mutation/README.md). It checks
that each list is well formed and that every `old` pattern still occurs EXACTLY ONCE in its file,
so a refactor cannot silently turn a mutation into a no-op.
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MUTATION_DIR = REPO / "scripts" / "mutation"
BACKEND = REPO / "backend"
LISTS = sorted(MUTATION_DIR.glob("*.json"))
REQUIRED = {"id", "desc", "file", "old", "new", "tests"}


def norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def test_there_are_mutation_lists() -> None:
    assert {p.stem for p in LISTS} >= {"commit3", "commit4"}


@pytest.mark.parametrize("path", LISTS, ids=lambda p: p.stem)
def test_each_list_is_well_formed(path: Path) -> None:
    muts = json.loads(path.read_text(encoding="utf-8"))
    assert muts, "empty list"
    ids = [m["id"] for m in muts]
    assert len(ids) == len(set(ids)), "duplicate ids"
    for m in muts:
        assert REQUIRED <= set(m), m["id"]
        assert m["old"] != m["new"], f"{m['id']}: mutation changes nothing"
        for t in m["tests"].split():
            assert (BACKEND / t).exists(), f"{m['id']}: missing test file {t}"


@pytest.mark.parametrize("path", LISTS, ids=lambda p: p.stem)
def test_every_pattern_still_occurs_exactly_once(path: Path) -> None:
    for m in json.loads(path.read_text(encoding="utf-8")):
        target = BACKEND / m["file"]
        assert target.exists(), f"{m['id']}: no such file {m['file']}"
        occurrences = norm(target.read_text(encoding="utf-8")).count(norm(m["old"]))
        assert occurrences == 1, (
            f"{m['id']} ({m['desc']}): pattern occurs {occurrences}x in {m['file']}; "
            "update scripts/mutation/" + path.name
        )


def test_the_runner_lists_without_running_anything() -> None:
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, str(MUTATION_DIR / "run.py"), "--list"],
        capture_output=True,
        text=True,
        timeout=60,  # a cold Python start alone can take ~11 s
    )
    assert out.returncode == 0
    assert "commit4:" in out.stdout and "R1" in out.stdout
