"""Manual mutation testing: break the code on purpose, one defect at a time, and check that the
test suite notices. See README.md in this directory.

    python scripts/mutation/run.py commit4               # every mutation in commit4.json
    python scripts/mutation/run.py commit4 --only R1,R2  # just those
    python scripts/mutation/run.py --list                # what lists / mutations exist

Standard library only. Works from a clean checkout: the backend directory is found relative to this
file, and the backend's own virtualenv is used for pytest if it exists.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
CRLF, LF = "\r\n", "\n"


def backend_python() -> str:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = BACKEND / rel
        if candidate.exists():
            return str(candidate)
    return sys.executable


def list_files() -> list[Path]:
    return sorted(HERE.glob("*.json"))


def load(name: str) -> list[dict]:
    path = HERE / (name if name.endswith(".json") else f"{name}.json")
    if not path.exists():
        sys.exit(f"no such mutation list: {path.name} (try --list)")
    return json.loads(path.read_text(encoding="utf-8"))


def git_dirty(paths: list[str]) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "--", *paths],
        capture_output=True,
        text=True,
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("list_name", nargs="?", help="name of a *.json list in this directory")
    ap.add_argument("--only", help="comma-separated mutation ids to run")
    ap.add_argument("--list", action="store_true", help="list available lists and mutations")
    ap.add_argument("--timeout", type=int, default=240, help="seconds per mutant (default 240)")
    ap.add_argument(
        "--allow-dirty",
        action="store_true",
        help="run even if the files to be mutated have uncommitted changes (recovery by "
        "`git checkout` will then lose them if a run is killed)",
    )
    args = ap.parse_args()

    if args.list or not args.list_name:
        for path in list_files():
            muts = json.loads(path.read_text(encoding="utf-8"))
            print(f"{path.stem}: {len(muts)} mutations")
            for m in muts:
                print(f"  {m['id']:<4} {m['desc']}")
        return 0

    muts = load(args.list_name)
    only = set(args.only.split(",")) if args.only else None
    chosen = [m for m in muts if not only or m["id"] in only]
    if only and len(chosen) != len(only):
        sys.exit(f"unknown ids: {sorted(only - {m['id'] for m in chosen})}")

    targets = sorted({m["file"] for m in chosen})
    dirty = git_dirty([f"backend/{t}" for t in targets])
    if dirty and not args.allow_dirty:
        print("Refusing to run: these files have uncommitted changes:", file=sys.stderr)
        print("\n".join("  " + ln for ln in dirty), file=sys.stderr)
        print(
            "Commit or stash them (a killed run is repaired with `git checkout -- backend`), "
            "or pass --allow-dirty.",
            file=sys.stderr,
        )
        return 2

    py = backend_python()
    caught = survived = timeouts = not_applied = 0
    print(f"python: {py}\nlist:   {args.list_name}  ({len(chosen)} mutations)\n", flush=True)

    for m in chosen:
        f = BACKEND / m["file"]
        original = f.read_bytes()
        text = original.decode("utf-8")
        crlf = CRLF in text  # match the file's own line endings (Windows working copies)
        old = m["old"].replace(LF, CRLF) if crlf else m["old"]
        new = m["new"].replace(LF, CRLF) if crlf else m["new"]
        n = text.count(old)
        if n != 1:
            print(f"{m['id']:<4} {m['desc']:<62} NOT APPLIED (pattern occurs {n}x)", flush=True)
            not_applied += 1
            continue
        try:
            f.write_bytes(text.replace(old, new).encode("utf-8"))
            try:
                r = subprocess.run(
                    [py, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *m["tests"].split()],
                    cwd=BACKEND,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                )
            except subprocess.TimeoutExpired:
                print(f"{m['id']:<4} {m['desc']:<62} TIMEOUT (hung > {args.timeout}s)", flush=True)
                timeouts += 1
                continue
            tail = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
            first_fail = next((ln for ln in r.stdout.splitlines() if ln.startswith("FAILED")), "")
            ok = r.returncode != 0
            caught += ok
            survived += not ok
            print(
                f"{m['id']:<4} {m['desc']:<62} {'CAUGHT  ' if ok else 'SURVIVED'} {tail}",
                flush=True,
            )
            if first_fail:
                print(f"       first failure: {first_fail[:160]}", flush=True)
        finally:
            f.write_bytes(original)  # always restore, even on Ctrl+C

    print(f"\ncaught={caught} survived={survived} timeout={timeouts} not_applied={not_applied}")
    # A survivor or a pattern that no longer applies is a problem worth failing the run for.
    return 1 if (survived or not_applied) else 0


if __name__ == "__main__":
    raise SystemExit(main())
