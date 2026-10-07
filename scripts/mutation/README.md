# Mutation checks

Passing tests prove little unless they fail when the code is wrong. This tool checks that, for the
security- and correctness-critical paths: it introduces **one deliberate defect at a time** (a
*mutation*), runs the tests that are supposed to guard that behaviour, and reports whether they
noticed.

* **CAUGHT** - at least one test failed. Good.
* **SURVIVED** - every test still passed. Either a test is missing or too weak, or the mutation is
  *equivalent* (it does not change behaviour; document why, as the PyJWT `alg=none` case was).
* **NOT APPLIED** - the code the mutation targets no longer looks like the pattern. Update the list.
* **TIMEOUT** - the mutant made the tests hang (reported separately; usually still a detection).

## Run it

From the repo root, with the backend dependencies installed (see the main README) and Postgres
(and, for the event tests, Redpanda) running:

```text
python scripts/mutation/run.py --list                  # lists and mutation ids
python scripts/mutation/run.py commit4                 # the whole commit-4 list
python scripts/mutation/run.py commit4 --only R1,R2    # selected mutations
python scripts/mutation/run.py commit5 --timeout 400   # Kafka-backed suites are slower
```

The runner is standard-library only. It finds `backend/` relative to itself and uses
`backend/.venv` for pytest when present (otherwise the Python running it). It exits non-zero if any
mutation survives or no longer applies.

A full list takes minutes: each mutation runs its tests once (`pytest -x`, stops at the first
failure, and the first failing test is printed so you can see it failed for the *right reason*).

## Safety

* The tool edits source files in place and restores the original bytes in a `finally` block,
  including on Ctrl+C.
* It **refuses to run if the files it will mutate have uncommitted changes**, so a killed run can
  always be repaired with `git checkout -- backend`. Use `--allow-dirty` only if you accept that.
* Do not run anything else against the same working tree or the `ops_test` database while it runs:
  other test runs would see the broken code, and the suites rebuild `ops_test`.
* Line endings: patterns are written with `\n`; the runner converts to the file's own style, so it
  works on Windows (CRLF) and Linux/macOS (LF) checkouts.

## The lists

| File | Covers |
|---|---|
| `commit3.json` | Tool executor pipeline: policy gate, authorization, logging of denied calls, argument size caps, rollback on failure, agent visibility of admin tools, owner scoping |
| `commit4.json` | Refunds: unique constraint, order and customer locks, `request_hash`, policy gate, provider idempotency key, outbox atomicity, provider call outside a transaction, canonical hashing, velocity statuses, reconcile re-check, self-approval |
| `commit5.json` | Outbox relay and consumer: marking published only after the broker acknowledges, `SKIP LOCKED`, per-aggregate ordering, backoff and parking, producer durability settings, offset-commit order, consumer de-duplication, poison-message handling, no publishing from request handlers, the event field allow-list |

`tests/test_mutation_lists.py` runs in the normal suite and checks that every pattern in every list
still occurs **exactly once** in its file, so the lists cannot silently rot when code is refactored.

## Adding a mutation

Append an object to a list:

```json
{"id": "R7", "desc": "what is broken",
 "file": "app/path/to/file.py",
 "old": "exact text to replace (must occur exactly once)",
 "new": "replacement text",
 "tests": "app/tests/test_one.py app/tests/test_two.py"}
```

Make the replacement use only names that already exist in the file: a `NameError` makes the tests
fail for the wrong reason (the printed first failure will reveal it). Prefer replacing a
condition with `False` or removing a statement, over rewriting logic.
