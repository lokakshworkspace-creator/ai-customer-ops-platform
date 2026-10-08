# Phase 2 report

Status report for Phase 2: policy engine, tool registry and executor, idempotent refunds with approvals, a (simulated) payment provider, a reconcile job, and the transactional-outbox relay and consumer. Figures were measured on 2026-10-08 against the code at commit `57fb3cc` (every later commit changes documentation only), on the development machine (Windows 11, Python 3.13, Docker containers for Postgres, Redis and Redpanda). Where something could not be verified it is marked **unverified**.

- [Summary](#summary)
- [What was built](#what-was-built)
- [Commits](#commits)
- [Test summary](#test-summary)
- [Mutation testing](#mutation-testing)
- [The live demo](#the-live-demo)
- [Problems found along the way](#problems-found-along-the-way)
- [Open items going into Phase 3](#open-items-going-into-phase-3)
- [How this was verified](#how-this-was-verified)

## Summary

- A refund request now goes through **validate → authorize → policy → execute → log** in one executor. The policy engine (pure functions over `rules.yaml`) answers `ALLOW`, `REQUIRE_APPROVAL` or `DENY` with a reason code; a denied request creates no refund, but is logged.
- Refunds are **idempotent in the database** (`UNIQUE(requested_by, idempotency_key)` plus a request hash). Ten simultaneous identical requests make exactly one refund and one provider payout.
- The payment provider is called with **no transaction and no lock held**; an unknown outcome leaves the refund `processing` for the reconcile script.
- Every state change writes an **outbox** row in the same transaction. A relay publishes in per-refund order; a consumer de-duplicates in the same transaction as its effect and dead-letters poison messages.
- **9 endpoints** (5 new refund and approval routes plus the 4 from Phases 0–1), **643 tests** (642 pass, 1 is POSIX-only and skipped), **56 mutation checks, all caught**, and a repeatable live demo.
- Not done on purpose (Phase 3 and later): the LLM agent, RAG, the UI, a real payment provider, login rate limiting.

## What was built

| Area | What exists |
|---|---|
| Policy | `app/policy/`: `engine.py` (pure functions: refund request, refund decision, cancel, ticket, read, action gate), `rules.py` (loader: rejects unknown keys, unknown statuses and floats), `rules.yaml` (auto-approve limit 500.00 INR, 30-day order age, 3 refunds per 30 days, refundable statuses, valid and auto-approvable reasons, admin-only and separation-of-duties actions). `DENY` > `REQUIRE_APPROVAL` > `ALLOW` |
| Tools | `app/tools/`: registry (7 tools: `get_order`, `list_orders`, `create_ticket`, `cancel_order`, `request_refund`, `approve_refund`, `reject_refund`; roles; `agent_visible`, false for the two admin tools), strict input schemas (unknown fields forbidden), executor with a start row committed first, capped `arguments`/`result` |
| Refunds | `app/modules/refunds/`: idempotent insert, canonical request hash, facts builder for the policy, payment provider interface and `SimulatedPaymentProvider` (ledger, latency, injected failures, lost responses), three-phase payout, `reconcile.py` |
| API | `POST /api/v1/refunds`, `GET /api/v1/refunds/{id}`, `GET /api/v1/approvals`, `POST /api/v1/approvals/{id}/approve`, `POST /api/v1/approvals/{id}/reject` (contract: [`refunds-api.md`](refunds-api.md)) |
| Database | Migration `0002` (hand-reviewed): per-user refund idempotency and `request_hash`, new refund/tool-call states, `tool_calls` size `CHECK`s, `outbox_events` identity `seq`, backoff and parking columns, `processed_events` table. 12 tables in total |
| Concurrency | `app/db/locking.py`: one lock order (order → user → refund → approval), `FOR NO KEY UPDATE`, documented in the file |
| Events | `app/events/`: `add_event` (ids-only allow-list), envelope v1, relay (`SKIP LOCKED`, per-aggregate order, `acks=all`, idempotent producer, backoff, parking), notification consumer (de-duplication, dead-letter topic), `outbox_admin.py` to un-park, `--once` hard deadline (exit 3) ([`events.md`](events.md)) |
| Scripts | `reconcile_refunds.py` (refuses the simulated provider without `--allow-simulated`), `outbox_admin.py`, `kafka_smoke.py`, `demo_phase2.py`, `demo_cleanup.py`, `scripts/mutation/` |
| Test tooling | `committed_db` fixture and guarded `TRUNCATE` (test database only), `kafka` marker applied by a `conftest` hook, `pytest-timeout` (180 s), real-process tests, mutation lists with a test that keeps them valid |
| Dependencies added | `aiokafka`, `pyyaml` (runtime); `pytest-timeout` (dev). All approved beforehand |

## Commits

20 commits, 2026-10-07 and 2026-10-08, in order:

| Commit | What |
|---|---|
| `ed4b6ab` | Housekeeping: migration `0002`, Redpanda advertises `127.0.0.1`, `aiokafka` + `pyyaml`, Kafka smoke script |
| `0e1c107` | Policy engine, `rules.yaml`, table-driven boundary tests |
| `f491c56` | Tool registry and executor, order and ticket tools, outbox writer |
| `2314064` | Refunds: idempotent `POST /refunds`, approve/reject, payouts outside transactions, reconcile script, lock order, concurrency tests |
| `b69a457` | Reconcile refuses the in-memory simulated provider unless `--allow-simulated` |
| `e8bbbe1` | Mutation runner moved into `scripts/mutation/` with README, lists, and a list-validity test |
| `8bfed7b` | Outbox relay and idempotent notification consumer, tests against the real Redpanda |
| `5a497a5` | Tighter poison-message test (found by mutation E10) |
| `568a385` | Hard `--once` deadline; automatic `kafka` marker; README test notes |
| `97bed37` | Deadline tests bounded so a broken deadline fails fast (found by mutation E13) |
| `b7ab4d2` | `scripts/demo_phase2.py`, the repeatable end-to-end demo |
| `aa94c52` | Real-process test timeouts raised to 60 s (cold starts), application deadlines kept short |
| `8de6775` | The demo refuses to run unless `APP_ENV=dev` |
| `4e35225` | `scripts/demo_cleanup.py` (dry run by default, dev only) with tests |
| `994dd06` | `pytest-timeout`, 180 s per test |
| `bc449a9` | Mutation lists `commit2.json` and `cleanup.json`; per-list counts |
| `57fb3cc` | Test pinning what `pytest-timeout` does on Windows |
| `79a1b91`, `9ac1520`, `c99e367` | README, `architecture.md`, `decisions.md` refresh |

(This report is the 21st.)

## Test summary

**`pytest --collect-only -q` reports 643 tests. Full run: 642 passed, 1 skipped, 229 s (3 min 49 s).** Counts are pytest items (each parametrised case counts separately). Ruff lint and format checks are clean.

| Selection | Result | Time |
|---|---|---:|
| `pytest` (everything) | 642 passed, 1 skipped | 229 s |
| `pytest -m "not kafka"` (Postgres only) | 615 passed (28 deselected) | 120 s |
| `pytest -m "not kafka"` with the Redpanda container **stopped** | 615 passed | 117 s |
| `pytest -m kafka` | 27 passed, 1 skipped | 124 s |

The skipped test sends `SIGTERM` to a child process and only runs on POSIX. The 28 `kafka` tests are 12 in `test_outbox_relay.py`, 9 in `test_event_consumer.py`, 3 each in `test_event_processes.py` and `test_events_e2e.py`, and 1 in `test_once_deadline.py`.

| Area | Files | Tests |
|---|---|---:|
| Phase 0–1 (health, request id, config, security, login, auth deps, orders authz, seed) | `test_health`, `test_request_id`, `test_config`, `test_security`, `test_auth_login`, `test_auth_deps`, `test_orders_authz`, `test_seed` | 119 |
| Schema and migrations | `test_schema` (34, was 11), `test_migration_0002` (data backfill) | 35 |
| Policy engine and rules | `test_policy_engine`, `test_policy_rules` | 97 |
| Tool registry and executor | `test_tool_registry`, `test_tool_executor` | 52 |
| Refunds | `test_refunds_api` (65), `test_refund_hashing` (40), `test_refund_facts`, `test_refunds_concurrency`, `test_refund_atomicity`, `test_payment_provider`, `test_refund_reconcile`, `test_reconcile_script` | 169 |
| Approvals | `test_approvals_api` | 19 |
| Events | `test_event_schema` (44), `test_outbox_relay`, `test_event_consumer`, `test_outbox_admin`, `test_event_processes`, `test_once_deadline`, `test_events_e2e`, `test_no_direct_publish` | 102 |
| Test tooling and dev-only scripts | `test_pytest_markers`, `test_mutation_lists`, `test_dev_only_scripts`, `test_demo_cleanup` | 50 |
| **Total** | 35 test files | **643** |

What the new security-relevant tests cover:

- **Policy:** the boundaries (exactly 500.00 is allowed, 500.01 needs approval; a 30-day-old order is allowed, 30 days and 1 second is not; the 3rd refund in 30 days is allowed, the 4th needs approval; a refund of one paisa more than remains is denied), every reason code, rule overrides taking effect (the engine follows the file, not hard-coded numbers), `DENY` beating the others, self-approval, and a bad `rules.yaml` refusing to load.
- **Executor:** every outcome writes a `tool_calls` row (denied, invalid, failed, succeeded), unknown fields are rejected, caller identity cannot be supplied in arguments, argument and result size caps, rollback on failure, admin tools are not `agent_visible`.
- **Refunds:** replay (`200`) versus conflict (`409`), the same key from two users gives two refunds and two provider ledger entries, a denial creates no row, a declined payout is `201 failed`, an unknown outcome is `202 processing` and is settled by reconcile, canonical hashing (`250` equals `250.00`), HTTP-level tests through the real `get_current_user`, a customer cannot see another's refund (`404`).
- **Concurrency (separate connections, real commits):** ten identical requests make one refund and one provider entry; velocity and remaining-amount limits hold under contention; an overlapping request and approve do not deadlock.
- **Events:** the relay never marks a row published before the broker acknowledges, never publishes an event ahead of an earlier unpublished one for the same refund, backs off, parks, and can be un-parked; the consumer applies each event once, survives redelivery and a restart, and dead-letters poison messages (including a trailing one); events with unlisted fields cannot be written or read; nothing but the event modules may produce to Kafka.

## Mutation testing

`scripts/mutation/run.py` introduces one deliberate defect at a time and requires the named tests to fail (details: [`scripts/mutation/README.md`](../scripts/mutation/README.md)). **56 mutations in five lists, all caught, none survived, none hung, none failed to apply**, run on the final code today:

| List | Covers | Mutations | Caught |
|---|---|---:|---:|
| `commit2.json` | Policy engine | 8 | 8 |
| `commit3.json` | Tool executor | 10 | 10 |
| `commit4.json` | Refunds, locking, payouts | 14 | 14 |
| `commit5.json` | Relay, consumer, deadline, marker | 14 | 14 |
| `cleanup.json` | `demo_cleanup.py`, the dev-only refusal | 10 | 10 |
| **Total** | | **56** | **56** |

The value of the exercise was the weak tests it exposed on the first run, all fixed before the list went green:

- **R1:** deleting the `UNIQUE` constraint survived, because the customer lock serialised everything and hid it. New test with one admin, one key and two customers.
- **R2:** a mutation hung because a paused task kept row locks, which blocked the next teardown `TRUNCATE`. Teardown now releases in a `finally`.
- **E10:** the poison-message test missed a trailing poison message and a restart. Tightened.
- **E13:** "the `--once` deadline never fires" was only "caught" by a 300 s hang. The deadline tests are now bounded with `wait_for`, so a broken deadline fails fast.
- **C6:** a cleanup test claimed to cover a real admin's refund of a demo order and did not. Case added.

## The live demo

`scripts/demo_phase2.py` starts the API, the relay and the consumer as three processes against the dev stack, runs four scenarios, prints the refund, outbox and notification state after each, and stops everything by PID. It creates its own customer, admin, delivered orders and succeeded payments each run (insert only, unique keys), so it does not depend on the seeded data and every run gives identical outcomes. The last run (today, 18 s, `17/17 checks passed`) ended with:

```text
OUTCOMES {"a": {"http": 201, "notifications": ["refund.requested", "refund.approved", "refund.completed"], "policy": "REFUND_ALLOWED", "status": "succeeded"},
          "b": {"http_approve": 200, "http_request": 202, "notifications": [...same three...], "policy": "REFUND_ABOVE_AUTO_LIMIT", "status_final": "succeeded", "status_request": "pending_approval"},
          "c": {"different_payload_http": 409, "http": 200, "new_rows": [0, 0, 0], "replayed": true},
          "d": {"code": "AMOUNT_EXCEEDS_ORDER", "http": 403, "new_rows": [0, 0, 0]}, ...}
```

(a) 250.00 refund paid at once; (b) 700.00 refund waits for a different admin, then is paid; (c) the same key replays with no new rows, a different payload gives `409`; (d) a 50000.00 refund is denied, creates nothing, and is in `tool_calls`. Three earlier runs gave the same outcomes, and the dev database now holds 5 demo runs (the cleanup dry run lists 10 demo users and 200 rows). `scripts/demo_cleanup.py --apply` has **not** been run.

## Problems found along the way

- **Migration `0002` backfill order.** Updating old rows before dropping the old `CHECK` failed; a data test with real old-format rows caught it. Fixed: drop the `CHECK`, backfill, re-create it (downgrade symmetric). Also `op.f()` is needed for `CHECK` names in `drop_constraint`.
- **Overflow:** `12345678901.00` exceeded `NUMERIC(12,2)`; the API now bounds the amount at `9999999999.99`.
- **ORM objects expire on rollback,** which broke code that read a user after a rolled-back call. Routers now copy the user into a plain `Actor` first.
- **`localhost` on Windows:** IPv6 first costs ~2 s per connection, and binding Docker ports to `127.0.0.1` made `/health` return `503`. All URLs and defaults use `127.0.0.1`.
- **Cold starts.** One early event test run took 33.8 s. The cause is the first import after idle: the virtualenv's 4,797 files are read cold, ~10.9 s versus 1.4 s warm. Real-process tests allow 60 s; the application-level `--deadline` they pass stays short.
- **One unexplained stall.** A consumer subprocess once took 90 s; it was not reproduced in 23 later runs. The `--once` deadline now turns such a hang into exit 3 with a message that names what it was waiting on.
- **One unexplained slow run.** One run of the fast suite reported 4214 s (70 min) for tests that take about 2 minutes, with all tests passing. Not reproduced (99–124 s in the five runs since); probably the machine sleeping, not confirmed. `pytest-timeout` now guards against hangs, but on Windows it ends the whole run rather than failing one test (a test pins this).

## Open items going into Phase 3

Status of the Phase 0–1 open items:

| Item | Status |
|---|---|
| Per-user refund idempotency bound to the request | **Done** (migration `0002`, decision 13) |
| Review the status enums before real data | **Done** (refund `pending_approval`/`processing`/`rejected`, tool-call states) |
| Redpanda advertised listener `127.0.0.1`, tested with a real producer | **Done** (smoke script, relay and consumer tests) |
| Mutation script not in the repo | **Done** (`scripts/mutation/`) |
| `outbox_events` schema only | **Done**; `workflow_runs` is still unused |
| Login rate limiting | **Open** |
| Token revocation / refresh | **Open** |
| `audit_logs` append-only is not enforced | **Open**, and `demo_cleanup.py` deliberately deletes demo audit rows (dev only) |
| `alembic check` does not compare `CHECK` constraints | **Open** (the migration data test and schema tests cover the new states) |
| No CI | **Open** |

New items:

1. **The payment provider is simulated.** Its ledger is in memory, so exactly-once across restarts is only demonstrated by sharing one instance; the reconcile script refuses it unless told otherwise. A real provider needs an adapter and a real idempotency story.
2. **The notification consumer only logs.** It proves at-least-once, de-duplicated consumption, not delivery to a person.
3. **No HTTP route for tickets or order cancellation,** and no caller of the tools other than the refund/approval routes, until the agent exists (Phase 3). The agent must call the executor with an authenticated `Actor`; `agent_visible` is the guard against showing it the admin tools.
4. **A parked outbox event blocks later events of the same refund** until an operator un-parks it. There is no alert for parked events yet (Phase 6).
5. **Redis is still unused.** The idempotency fast path was deliberately left out (the database decides).
6. **Approvals do not expire** (`approvals.expires_at` exists but no application code sets or enforces it).
7. **Unverified:** Linux/macOS (README commands and `pytest-timeout`'s signal method), a real long-running relay/consumer pair outside the demo for hours, and Redpanda behaviour under broker restarts mid-publish (the producer settings are tested by mutation, not by killing the broker).

## How this was verified

All run today (2026-10-08) on the committed code:

- **Test counts and timings:** `pytest --collect-only -q` (643; `-m "not kafka"` 615; `-m kafka` 28), the full suite once, `-m "not kafka"` five times (99 s, 124 s, 123 s, 120 s, and 117 s with the Redpanda container stopped and started again afterwards), `-m kafka` once.
- **`pytest-timeout` on Windows:** a hanging test between passing ones, run with `-o timeout=3`: the earlier tests passed, the run printed a stack dump at the hanging test and exited with code 1 after about 3 s, the hanging test was not reported as failed and the later tests did not run.
- **Mutation lists:** all five run with the runner from a clean tree (56 caught); the working tree was clean afterwards.
- **Documented commands:** `docker compose ps`, `alembic upgrade head` (already at `0002`), the seed (already seeded, no-op), `reconcile_refunds.py --dry-run`, `outbox_admin.py list`, `relay --once`, the consumer's `--once`, `kafka_smoke.py`, `demo_phase2.py`, and the `demo_cleanup.py` dry run.
- **Diagrams and links:** the seven Mermaid diagrams in `architecture.md` were parse-checked; relative links and `#anchors` in the README, `architecture.md` and `decisions.md` were checked with a script that mimics GitHub's slug rules. The ER diagram and the module tree are generated from the SQLAlchemy metadata and `git ls-files`.
- **Not verified:** how the diagrams look when rendered, Linux/macOS, and anything involving a real payment provider.
