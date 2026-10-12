# Phase 6 report

Status report for Phase 6, built as **hardening**: login rate limiting, token revocation with a logout, approval expiry, and observability. Figures were measured on 2026-10-12 on the development machine (Windows 11, Python 3.13, Node 24.15, Docker containers for Postgres, Redis and Redpanda) against the code at commit `c8f70ce` (the commits after it are documentation). Where something could not be verified it is marked **unverified** or listed under "What was not verified", and who ran what is stated.

- [Summary](#summary)
- [What was built](#what-was-built)
- [Commits](#commits)
- [Test summary](#test-summary)
- [Mutation testing](#mutation-testing)
- [The demo](#the-demo)
- [Verification: who saw what](#verification-who-saw-what)
- [What was not verified](#what-was-not-verified)
- [Problems found along the way](#problems-found-along-the-way)
- [Deliberately not included](#deliberately-not-included)
- [Open items](#open-items)
- [How this was verified](#how-this-was-verified)

## Summary

- **Login rate limiting (Redis).** Failed logins are counted per **email + IP**; after 5 in 15 minutes the pair gets `429` with `Retry-After`, before any password work (the right password is refused too), and a success resets the counter. **It fails open**: with Redis down logins keep working and a warning is logged. **It is per email and IP, so a distributed attack from many IPs on one email is a known limit.**
- **Token revocation (Postgres, not a Redis denylist).** Every token has a `jti`; `POST /auth/logout` revokes the calling token only; a revoked token is `401` on every authenticated route (a test walks every route). The table is Postgres because revocation is security state and Redis is disposable.
- **Approval expiry.** A new process, `python -m app.jobs.sweeper`, expires approvals still pending after `APPROVAL_TTL_HOURS` (48): the approval and its refund become `expired`, a `refund.expired` event goes through the outbox, the resume consumer tells the customer, and the admin Expired tab fills. **Deciding an expired or overdue approval is refused** (`409 APPROVAL_EXPIRED`, approve and reject) with no gap before the sweeper's next run. **The sweeper must be running**, and the first sweep expires every pending approval older than the limit, including old ones.
- **Observability.** The request id was already on every response and log line; it is now **also on a crash's `500` and in the crash's log** (a real gap, found by a test: Starlette built that response outside the middleware). `/health` was already right and gained tests. A new admin-only, read-only `GET /api/v1/admin/pipeline` shows a stopped relay, resume consumer or sweeper, and is **deliberately not part of `/health`**.
- **One thing in my own plan was wrong, and was caught before it shipped.** The plan said to set `TRUSTED_PROXY_HOPS=1` behind the Next.js proxy. Checked against a real Next.js dev server: its rewrite forwards the client's own `X-Forwarded-For` unchanged and adds none, so with 1 a client dodges the limiter by sending its own header (six rotating spoofed attempts, six `401`s, never blocked). The right setting there is 0; the code comment, `.env.example` and the docs say so (`c8f70ce`).
- **No change to the executor, the policy engine, the idempotency code or `rules.yaml`** (`git diff ffb2fc0..HEAD` over those paths and the refund service, repository and hashing is empty). The decision handlers' own guard (`tools/handlers/refunds.py`) gained the deadline check, as agreed.
- **Numbers, measured today:** 1279 tests collected (1248 fast, 31 `kafka`), **1278 passed, 1 skipped in 4 min 47 s**; **16 tables**, **15 endpoints**; the new `phase6` mutation list **23 of 23 caught**, and `phase5` (9), `commit3` (10) and `commit4` (14) re-run and caught; the demo **38/38 twice with identical `OUTCOMES`**.

## What was built

| Area | What exists |
|---|---|
| Login limiter (`core/login_limiter.py`, `api/routers/auth.py`, `api/deps.py`) | `LoginLimiter`: key = SHA-256 of (normalised email, IP), counter and TTL in one atomic Lua script, `retry_after` / `record_failure` / `reset`, every Redis call under a 0.5 s timeout and `except Exception` (fails open, warning logged). `client_ip`: the socket peer, or the `X-Forwarded-For` entry `TRUSTED_PROXY_HOPS` from the right (default 0), falling back to the peer on a missing, short or malformed header. `get_login_limiter` is a dependency, so tests give every test its own limiter and key prefix. Settings: `LOGIN_MAX_FAILURES`, `LOGIN_WINDOW_SECONDS`, `LOGIN_LIMITER_TIMEOUT_S`, `TRUSTED_PROXY_HOPS` |
| Revocation (`db/models/revoked_token.py`, migration `0006`, `modules/users/tokens.py`, `core/security.py`, `api/deps.py`, `POST /auth/logout`) | `revoked_tokens(jti PK, user_id FK ON DELETE CASCADE, expires_at, revoked_at)` with an index on `expires_at`; `jti` required in every token (`decode_token` returns `TokenClaims`); `get_current_token` checks the table before the user lookup; logout is `204`, audited, revokes only the calling token; both pages' Sign out call it |
| Expiry (`modules/refunds/expiry.py`, `jobs/sweeper.py`, migration `0007`) | `expire_one`: locks order, user, refund, approval, re-checks status and deadline under them, expires approval and refund, writes `refund.expired` and an audit row (no actor), commits; `due_approval_ids` (no locks, oldest first); `is_past_deadline`. The sweeper: one approval per transaction, batches, then purges expired revocations; `--once` with a deadline (exit 3); a failing approval is skipped and retried next sweep. Settings: `APPROVAL_TTL_HOURS`, `SWEEPER_INTERVAL_S`, `SWEEPER_BATCH_SIZE` |
| The refund status `expired` (migration `0007`) | The `CHECK` replaced under the same name (one per enum); downgrade maps `expired` to `rejected`. `rules.yaml` lists the statuses that hold money and count toward velocity by inclusion, so an expired refund releases both with no policy change. `refund.expired` is in `EVENT_DATA_FIELDS`, the notification template and `docs/events.md`; the resume consumer handles it; the customer-facing summary and one prompt sentence say "nothing was paid" |
| The deadline guard (`tools/handlers/refunds.py`) | `_lock_for_decision` refuses an `expired` approval and a pending one past `created_at + APPROVAL_TTL_HOURS` (compared with `ctx.now`), for approve and reject, with `409 APPROVAL_EXPIRED` |
| Admin view (`modules/refunds/approvals_view.py`, `frontend/app/admin/page.tsx`) | `expires_at` is the deadline for a pending approval (computed by the backend) and the passed deadline for an expired one; the page shows "decide by" on a pending card and an "expired, nothing was paid" card |
| Observability (`api/middleware.py`, `modules/pipeline_health.py`, `api/routers/admin.py`) | The middleware catches an unhandled exception, logs it with its traceback and the id, answers `500 {"detail": "Internal server error", "request_id": …}` with the header. `GET /api/v1/admin/pipeline`: outbox backlog and parked events, decided-but-unfinished turns, overdue approvals; counts and ages only; `ok` or `attention` with reasons; the outbox through the ORM model (a test lets only the relay touch that table in SQL) |
| Demo, mutations | `scripts/demo_phase6.py` (38 checks); `scripts/mutation/phase6.json` (23 mutations) |

## Commits

Fifteen commits since the Phase 5 report (`ffb2fc0`): ten code commits (the plan's nine, plus a correction) and five documentation commits (this report is the last). Changes outside the docs, `ffb2fc0..c8f70ce`: 54 files, +3976 / −69 lines (the test files hold +2079 / −27 of them).

| Commit | What |
|---|---|
| `45ff95f` | (1/10) the login limiter, `TRUSTED_PROXY_HOPS`, per-test limiter isolation in `conftest.py`, tests |
| `67f5d59` | (2/10) migration `0006`, `jti` in every token, `get_current_token`, revocation tests incl. a route walk |
| `92b28f9` | (3/10) `POST /auth/logout`, both pages' Sign out, tests |
| `cc5b173` | (4/10) the refund status `expired`: migration `0007`, `refund.expired` in the schema, template and `events.md`, status checks, tests |
| `360f5d4` | (5/10) `expiry.py`, the sweeper, `APPROVAL_TTL_HOURS`, the deadline guard in the handlers; tests incl. races and a real process |
| `7359c1b` | (6/10) the resume consumer and the fallback finish an expired turn, the deadline in the approvals list, the admin page; a test through Redpanda |
| `da7d77f` | (7/10) the crash 500 with the request id, observability tests, the pipeline endpoint; mutation `P2` retargeted |
| `0d8d011` | (8/10) `phase6.json`, and two tests added so that mutants could not hide |
| `9f6be43` | (9/10) `scripts/demo_phase6.py` |
| `c8f70ce` | the `TRUSTED_PROXY_HOPS` guidance corrected after the experiment |
| `055bdec`, `38ef397`, `419af64`, `39d3c45`, and this report | Documentation: README (and `CLAUDE.md`), architecture, decisions 44–49, the mutation README and `refunds-api.md` |

## Test summary

`pytest --collect-only -q` reports **1279** tests: **1248** not marked `kafka` and **31** marked. The whole suite (Postgres, Redis and Redpanda up) at `9f6be43`: **1278 passed, 1 skipped in 4 min 47 s** (the skipped test needs POSIX); the one later code commit, `c8f70ce`, changed comments and `.env.example` only, and the fast part was re-run after it: **1248 passed in 2 min 53 s** (1107 in 2 min 36 s at the end of Phase 5). That is 142 more tests than the 1137 of the Phase 5 report; the ten new files hold 135 of them, and the other 7 are additions to existing files (three resume-consumer tests, one Redpanda test, one status parameter, …) not attributed per file. `ruff check` and `ruff format --check` are clean (229 files).

| File | Tests | What it covers |
|---|---:|---|
| `test_login_rate_limit.py` | 19 | `429` and `Retry-After` after the maximum; the right password refused while blocked; blocked attempts do no password work and do not extend the window; reset on success; per email and per IP; unknown emails counted like known ones; the window ending; a TTL on every counter and no email in the key; fail open with a dead Redis and with a hanging one; `client_ip` for every header shape (spoofed left entry, too few entries, malformed) |
| `test_token_revocation.py` | 18 | a route walk (no route authenticates around the check); a revoked token is `401` on 11 endpoints and a live one is not; one revoked token leaves the user's others alone; idempotent; a token without or with an over-long `jti` is refused; the purge removes only expired rows; deleting a user cascades |
| `test_logout.py` | 8 | `204`, the token dead everywhere, only that token, other users untouched, twice is `401` with one row, a token needed, the row holds the token's own id and expiry and is audited without the token, and **the revocation committed for real** (every request its own session) |
| `test_migration_0006.py` | 5 | the table's shape, primary key and cascade, the purge index, no CHECK added, the downgrade round trip, a linear chain |
| `test_migration_0007.py` | 5 | exactly one named CHECK that lists every enum value; an expired refund can be stored and an unknown status cannot; an expired refund holds no money and no velocity slot (real rows); the downgrade maps to `rejected` and the upgrade restores the CHECK |
| `test_refund_expired_status.py` | 8 | every event type has a notification template; the `refund.expired` schema carries no actor; the consumer reacts to it; the expired outcome text; the customer summary; the prompt sentence |
| `test_approval_expiry.py` | 25 | expiry writes the approval, refund, event and audit row; inside the limit nothing happens; twice changes nothing; a decided approval is never expired; the limit is a setting; money and velocity released; due ids; a swept and an overdue approval cannot be approved or rejected; races (a decision and the sweeper, two sweepers, many contenders on one order without a deadlock); the sweep, a failing approval, stop, a real `--once` process and its deadline; the event is in the same transaction |
| `test_admin_expiry_view.py` | 9 | the deadline on a pending approval and its setting, the Expired tab with its story, no deadline once decided, `409 APPROVAL_EXPIRED` over HTTP for expired and overdue |
| `test_observability.py` | 19 | a crash is a clean `500` with the id (header and body) and no exception text; logged once with traceback and id; the id on 401, 404, 405, 422, 429; bound to log lines and not leaked to the next request; one access line; the id in `tool_calls`, `audit_logs` and the outbox; `/health` for each dependency alone and both, a hanging dependency, no details leaked, no login and no limit |
| `test_pipeline_health.py` | 19 | admin only (403 for a customer and a forged claim, 401 without or with a revoked token); the relay (fresh, old, parked); the consumer (waiting is normal, just decided is not alarming, long after a final decision is, expired counts, approved-and-processing does not, an unusable checkpoint does not break it); the sweeper (and its grace); it writes nothing and leaks no ids; `/health` is untouched |

Existing tests changed only where the facts changed: the table count 15 to 16, the token claims (`jti`), `test_refund_facts.py` (one more status), a Phase 5 assertion that `expires_at` is null, the migration-head assertions (relaxed to "one head"), and `test_mutation_lists.py` (unchanged, but it caught the ambiguous `P2` pattern). `conftest.py` now overrides the limiter for every test.

## Mutation testing

`python scripts/mutation/run.py --list` shows nine lists and **116** mutations (agent 18, cleanup 12, commit2 8, commit3 10, commit4 14, commit5 14, phase5 9, **phase6 23**, rag 8). Run today, each once from a clean tree: **`phase6`: 23 of 23 caught**; and, because Phase 6 changed code they guard, **`phase5` 9 of 9, `commit3` 10 of 10 and `commit4` 14 of 14 caught**. The lists `agent`, `rag`, `cleanup`, `commit2` and `commit5` were **not** re-run (the test that checks every pattern still occurs exactly once passes for all nine lists).

| Id | Defect | First failing test |
|---|---|---|
| L1 | the limiter blocks one failure too late | `test_the_attempt_after_max_failures_is_429_with_retry_after` |
| L2 | a successful login does not reset the counter | `test_a_successful_login_resets_the_counter` |
| L3 | a failed login is not counted | `test_the_attempt_after_max_failures_is_429_with_retry_after` |
| L4 | Redis down fails closed | `test_redis_down_fails_open_and_login_still_works` |
| L5 | the key ignores the IP | `test_the_limit_is_per_ip` |
| L6 | the client-controlled left `X-Forwarded-For` entry is trusted | `test_client_ip[…spoofed…]` |
| L7 | the counter has no TTL | `test_the_counter_always_has_a_ttl_and_never_holds_the_email` |
| T1 | a revoked token is accepted | `test_a_revoked_token_gets_401_everywhere_and_a_live_one_does_not` |
| T2 | an over-long `jti` is accepted | `test_a_token_without_a_jti_is_refused` |
| T3 | logout does not commit | `test_the_revocation_is_committed_not_just_visible_inside_one_session` |
| X1 | the sweeper expires an approval not yet due | `test_an_approval_inside_its_limit_is_left_alone` |
| X2 | no `refund.expired` event | `test_a_stale_approval_expires_with_its_refund_an_event_and_an_audit_row` |
| X3 | the refund is left pending | same test |
| X4 | the handlers accept an overdue approval | `test_an_overdue_approval_the_sweeper_has_not_reached_yet_is_refused_too` |
| X5 | the handlers accept a swept approval (wrong error) | `test_a_swept_approval_cannot_be_decided` |
| X6 | the limit is read in days, not hours | `test_a_stale_approval_expires_with_its_refund_an_event_and_an_audit_row` |
| X7 | the sweeper does not purge revocations | `test_a_sweep_expires_everything_due_and_purges_old_revocations` |
| X8 | the consumer ignores `refund.expired` | `test_an_expired_event_finishes_the_turn_with_a_clear_expired_message` |
| O1 | the crash 500 has no id header | `test_an_unhandled_error_is_a_clean_500_that_still_carries_the_request_id` |
| O2 | the crash is not logged | `test_the_crash_is_still_logged_with_its_id_and_traceback` |
| O3 | a customer may read the pipeline snapshot | `test_a_customer_gets_403_and_nothing` |
| O4 | an expired approval is not a final decision | `test_a_turn_still_waiting_long_after_an_expiry_means_the_consumer_is_down` |
| O5 | approved-and-processing is treated as stuck | `test_an_approved_refund_still_processing_is_not_a_stuck_turn` |

Two tests were written **because a plausible mutant would have survived**: the logout-commit test (the per-test rolled-back session cannot see a missing commit, so it uses a client where every request has its own committed session) and the expired-turn case of the pipeline snapshot (the first draft only tested rejected and approved). The first draft of the mutation list also had a mutant that was equivalent to an existing check (requiring the `jti` claim, which the claim lookup already enforces), so it was replaced by the length check (`T2`). **`P2`** (Phase 5) had to be retargeted: the new pipeline route has a similar `_admin: Annotated[…]` line, so its pattern matched twice; it now includes the preceding `request: Request,` line, and it is still caught.

## The demo

**`scripts/demo_phase6.py`** (fake LLM and fake embedder forced for the processes it starts; a private topic prefix per run; refuses to run unless `APP_ENV=dev`, or if a relay is already running): (a) three wrong passwords block an email + IP (`429`, `Retry-After` inside the window, the right password refused too) while the same email from another IP and another email from the same IP are not blocked, and a successful login resets the counter; (b) a second API whose Redis is unreachable keeps logging users in and `/health` names redis; (c) logout makes the token `401` everywhere, the user's other token keeps working, and one `revoked_tokens` row exists; (d) an aged approval is refused by approve and by reject before the sweeper has run, the sweeper (a real process, `--once`) expires it (approval and refund `expired`, one `refund.expired` event), **before any `GET /workflows` the database shows the consumer finished the turn** ("expired without a decision"), an expired approval cannot be approved, it is on the Expired tab, and the customer can ask again; (e) with the consumer stopped by PID, a turn still waiting long after a final decision appears in `/admin/pipeline` (0 to 1), the customer's GET finishes it and the count goes back; (f) a request id is echoed in the header and appears in the API's log. **38/38 checks, run twice, the `OUTCOMES` lines identical.**

What it does to your data: it inserts its own `demo-` users and orders and **ages its own approvals in the database** (it writes `created_at` of its own rows instead of waiting 48 hours); every demo process runs with `APPROVAL_TTL_HOURS=99999`, so **the sweeper cannot reach anyone else's approvals** (the dev database's 14 older pending approvals were unchanged after both runs, checked); its relay publishes every unpublished dev outbox row to its private prefix (0 existed on both runs); the login counters it wrote in Redis were deleted (checked: none left). Ports 8125 and 8126 were used.

## Verification: who saw what

| Claim | Status |
|---|---|
| Everything under "How this was verified" below | **Run by me** on 2026-10-12, with the commands shown |
| The `X-Forwarded-For` behaviour of the Next.js dev proxy | **Seen by me**, with a real `next dev` and an echo upstream (what the upstream receives) and a real API (six rotating spoofed attempts, none blocked); scratch scripts, **not in the repository** |
| The dev database upgrade to `0007` | **Done by me**: `alembic upgrade head` (additive: a new table and a replaced `CHECK`); its 53 refunds and 14 pending approvals were unchanged |
| The Phase 6 changes in a browser | **Not seen by anyone yet** (see below) |
| Anything with the real Gemini models | **Not exercised**; everything uses the fake model and fake embedder |

## What was not verified

* **The Phase 6 page changes in a real browser**: Sign out through the API, the "decide by" line and the expired card on the admin page, and the `429` message on the login form. They pass `tsc` and `next build` only; there are no frontend tests. (Phase 5's Edge scripts are not in the repository and were not rerun.)
* **The sweeper's long-running loop** (`python -m app.jobs.sweeper` without `--once`: the interval sleep and the signal handling) was not run for any length of time. `--once`, the sweep itself, its stop flag and its deadline are tested, and `--once` is demonstrated.
* **A real Redis outage** (stopping the container while the API runs). Fail open is tested with a client pointed at a dead port, with a hanging fake, and by the demo's Redis-less API, but not by pulling the plug on a live connection.
* **The limiter behind a real reverse proxy that appends `X-Forwarded-For`** (nginx, a load balancer): the parsing is unit-tested for every header shape, but the only proxy observed was the Next.js dev proxy, and it appends nothing.
* **Concurrent login stress** on the limiter. The counter is one atomic Redis script, but 100 parallel logins were not run.
* **A real 48-hour expiry**: the tests and the demo age the rows (or pass a clock); nobody waited two days.
* **The assistant's new "expired" sentence** is guidance checked only against the scripted fake model; no real model saw it.
* **The mutation lists `agent`, `rag`, `cleanup`, `commit2` and `commit5`** were not re-run in Phase 6.
* **Migration `0007`'s downgrade** was exercised on the test database only, never on the dev one.

## Problems found along the way

1. **The `TRUSTED_PROXY_HOPS` plan was wrong.** Setting 1 behind the Next.js dev proxy would have let any client dodge the limiter (above). Found by testing the claim before documenting it; corrected in `c8f70ce`. The same experiment is why the dev setup's limit is effectively per email: every browser behind that proxy looks like `127.0.0.1`.
2. **A crash's `500` had no request id and no log line with one.** The first draft of the observability tests failed on exactly that; the middleware now catches, logs and answers cleanly (`da7d77f`).
3. **`capture_logs` lies when a logger was used earlier in the run** (structlog caches loggers on first use): two log tests passed alone and failed in the full suite. They use `caplog` now.
4. **A guard test blocked the pipeline snapshot** (only the relay may mention the outbox table in SQL). The snapshot reads it through the ORM model instead of the guard being loosened.
5. **A test double-booked an order.** `waiting_turn` always used the 1000.00 order, so a second 700 refund in one test was denied; it now takes an order.
6. **The first draft of the race test was not a race**: with both contenders legitimately past the deadline the outcome was deterministic. It was rewritten around what can really contend (two sweepers, and a mix of decisions and sweeps on one order, with a deadlock probe).
7. **A commit went in while ruff reported one over-long comment line** (my command separator let it through); the unpushed commit was amended.
8. **The route-walk test** needed to look inside this FastAPI version's included-router objects; it does, and has a floor so that an empty walk cannot pass.
9. **The default `node` here is v18.17**, and `tsc` fails under it; Node 24.15 was put first on `PATH` for the frontend commands only.
10. **Shell heredocs mangled several multi-line edits** again (an escape swallowed, a quote); the files were written with the editing tools or script files instead.

## Deliberately not included

- Admin access to customers' chat transcripts (workflows and conversations stay owner-only, administrators included).
- A rate limit on the chat endpoint beyond the hourly turn budget, "log out everywhere", refresh tokens, CAPTCHA or account lockout, and any alerting (the pipeline endpoint is read on demand; there is no metrics endpoint or dashboard).
- A defence against a **distributed** login attack (many IPs on one email): the limiter is per email and IP, by design.
- The answer-level evals and a re-ranker from the earlier plan, and the polish of Phase 7.
- Any change to the executor, the policy engine, the idempotency code or `rules.yaml`. Checked with `git diff --name-only ffb2fc0 HEAD` over `backend/app/tools/executor.py`, `backend/app/policy/` (which holds `rules.yaml`) and the refund `service.py`, `repo.py` and `hashing.py`: empty. The decision handlers' own guard in `tools/handlers/refunds.py` did change, as agreed.

## Open items

1. **Run the sweeper.** Nothing starts it for you; without it approvals never expire. The first sweep expires every pending approval older than 48 hours, including the dev database's 14 old ones (they move to the Expired tab, each with a `refund.expired` event).
2. **No alerting.** `/api/v1/admin/pipeline` shows a stopped relay, consumer or sweeper to whoever looks; nothing pages anyone, and there is no metrics endpoint.
3. **The dev setup's limiter is effectively per email**, so a known email can be kept blocked for 15 minutes by anyone (the lock-out trade-off); a proxy that sets `X-Forwarded-For` and `TRUSTED_PROXY_HOPS` removes it.
4. **A distributed attack on one email is not covered.**
5. **Sessions open at the upgrade end once** (tokens without a `jti` are refused).
6. **After Approve or Reject the card leaves the Pending tab with its confirmation** (Phase 5 item, still not fixed).
7. **Rejecting an overdue approval is refused**, so an admin cannot tidy one up after the deadline; it simply expires.
8. **Leftover rows in the dev database:** the `demo-*` users, orders and approvals from the two demo runs, plus the earlier Phase 5 ones; `scripts/demo_cleanup.py --apply` would remove them (a dry run by default; **not run**). The demo's expired approvals are aged 100000 hours, so they sort first on the Expired tab.
9. **Scratch scripts** for the `X-Forwarded-For` experiment are not in the repository.
10. **Carried over:** the real-model behaviours from Phase 4 remain unverified with the real model; the retrieval thresholds were tuned on the questions that measure them; the Phase 5 Edge scripts are not in the repository.

## How this was verified

| Check | Command or evidence | Result |
|---|---|---|
| Lint | `ruff check . ../scripts` and `ruff format --check . ../scripts` (from `backend/`) | clean (229 files formatted) |
| Frontend | `npm run typecheck`, `npm run build` (Node 24.15 first on `PATH`) | pass; routes `/`, `/_not-found`, `/admin`, `/icon.svg` |
| Test count | `pytest --collect-only -q` (from `backend/`) | 1279 collected; `-m kafka` 31, the rest 1248 |
| Whole suite | `pytest` (Postgres, Redis and Redpanda up) at `9f6be43` | 1278 passed, 1 skipped, 4 min 47 s |
| Fast suite | `pytest -m "not kafka"` at `c8f70ce` | 1248 passed in 2 min 53 s |
| Mutation | `python scripts/mutation/run.py phase6`; `phase5`; `commit3`; `commit4` | `phase6` 23 of 23, `phase5` 9 of 9, `commit3` 10 of 10, `commit4` 14 of 14 caught; no survivors; `--list`: 9 lists, 116 mutations |
| Demo | `scripts/demo_phase6.py --port 8125`, twice | 38/38 both times, identical `OUTCOMES` |
| Proxy experiment | a real `next dev` with an echo upstream, and with a private API at `TRUSTED_PROXY_HOPS=1` | the proxy appends nothing; spoofed rotating headers were never blocked |
| Protected code unchanged | `git diff --name-only ffb2fc0 HEAD` over the executor, `policy/`, the refund `service.py`, `repo.py`, `hashing.py` | empty |
| Dev database | `alembic upgrade head` (0005 to 0007); counts before and after | 53 refunds and 14 pending approvals unchanged; one `ck_refunds_refund_status` |
| ER diagram | regenerated from `Base.metadata` and compared with the committed block | the committed block plus `revoked_tokens` and its relationship |
| Mermaid | the real `mermaid` package (12.1.0, installed in a scratch directory outside the repository) over `architecture.md` | 13 diagrams parse; a deliberately broken one fails the same check |
| Module tree | generated from `git ls-files` and compared with the committed tree | the only differences were the new files, now included |
| Links | a script that mimics GitHub's heading slugs, over `README.md`, `CLAUDE.md` and every file in `docs/` | 163 relative links and `#anchors` checked, 0 problems |
