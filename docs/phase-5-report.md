# Phase 5 report

Status report for Phase 5: the admin approvals page and event-driven resume. An administrator decides a refund on a web page, through the endpoints that already existed, and the customer's waiting chat turn is finished **by an event** as soon as the decision is final. Figures were measured on 2026-10-12 on the development machine (Windows 11, Python 3.13, Node 24.15, Docker containers for Postgres, Redis and Redpanda) against the code at commit `e3b3781` (the commits after it are documentation). Where something could not be verified it is marked **unverified**, and who ran what is stated.

- [Summary](#summary)
- [What was built](#what-was-built)
- [Commits](#commits)
- [Test summary](#test-summary)
- [Mutation testing](#mutation-testing)
- [The demo and the real-browser check](#the-demo-and-the-real-browser-check)
- [Verification: who saw what](#verification-who-saw-what)
- [Problems found along the way](#problems-found-along-the-way)
- [Deliberately not included](#deliberately-not-included)
- [Open items](#open-items)
- [How this was verified](#how-this-was-verified)

## Summary

- **A decision finishes the waiting turn by event.** A new separate process, `python -m app.events.consumers.resume` (consumer `workflow-resume`), reads `refund.approved`, `refund.rejected`, `refund.completed` and `refund.failed` and finishes the chat turn that waits for that refund with one templated message. It takes ids from the event and everything else from the database, never calls the model or a tool, and moves no money. De-duplication is in the effect's transaction, the offset is committed after, and unprocessable messages are dead-lettered.
- **The Phase 3 read-time sync is kept as a fallback**, and it **can hide a dead consumer**: a stopped consumer is invisible from the customer's page because the next poll finishes the turn anyway. The demo reads the **database** before any `GET` to prove the consumer did the work, and then stops the consumer on purpose to show the fallback.
- **Two gaps closed.** The orchestrator looks once right after a turn enters `WAITING_FOR_HUMAN` (a decision made while the model was still writing its answer used to be missed), and a turn **stays waiting while an approved refund is still `processing`** (`waiting_for: "payout"`), finished later by `refund.completed` / `refund.failed`.
- **An admin page at `/admin`.** Pending, approved, rejected and expired tabs; each pending approval shows the refund, the order, the policy's review reasons and the requester's refund count; Approve and Reject use the **existing** endpoints and the backend's refusals are shown as they are; the signed-in user and a sign-out are shown; a customer sees "no access". The page decides nothing.
- **New and changed endpoints (13 in all, two new):** `GET /api/v1/auth/me` (role from the database), the enriched `GET /api/v1/approvals`, `waiting_for` on `GET /api/v1/workflows/{id}`, and an admin-only **staff knowledge-base lookup** (`GET /api/v1/admin/knowledge/search`; the audience comes from the authenticated role in code through `audiences_for_role`; audited without the question's text). The assistant's own search stays customer-only, even when an admin is chatting.
- **No change to the executor, the policy engine, the idempotency code or `rules.yaml`** (`git diff 42ffd59..HEAD` over those paths is empty; the only files changed under `app/tools/`, `app/policy/` and `app/modules/` are `tools/handlers/knowledge.py` (a helper moved to `rag/factory.py`) and the new `modules/refunds/approvals_view.py`). **Migration `0005`** adds one partial index; there is still **15 tables**.
- **Numbers, measured today:** 1137 tests collected (1107 fast, 30 `kafka`), **1136 passed, 1 skipped in 4 min 30 s**; 93 mutation checks in eight lists, the new `phase5` list (9) and `agent` A6 and A11 **all caught**; the demo **16/16 twice with identical `OUTCOMES`**; a real headless Edge 155 check with 0 console errors.
- Not done on purpose: approval expiry (deferred), admin access to customers' transcripts, login rate limiting, token revocation, Redis rate limiting.

## What was built

| Area | What exists |
|---|---|
| Resume core (`app/agent/resume.py`) | `finish_in_transaction` (the caller owns the transaction), `finish_waiting` (its own transaction), `sync_waiting` (the read-time fallback), `waiting_for(approval, refund)` returning `"approval"`, `"payout"` or `None`, and `outcome_text`. A row lock and a status re-check make it one idempotent transition; it only trusts ids the backend wrote into the checkpoint, and only a refund that belongs to the workflow's user |
| Race closure (`agent/orchestrator.py`) | After committing a turn that waits, `finish_waiting` is called once (8 added lines). It never calls the model or a tool |
| Resume consumer (`events/consumers/resume.py`) | Separate process on the `EventConsumer` base; topic `ops.refunds` (prefix configurable); handles the four events above and ignores the rest; finds the workflow with `status = 'waiting_approval' AND context ->> 'refund_id' = :id` and the event's customer; `--once` with the same deadline rules as the other consumers. Not a publisher (its only producer is for dead letters; `test_no_direct_publish.py` allows it for that) |
| Migration `0005` | `ix_workflow_runs_waiting_refund`: a partial expression index on `(context ->> 'refund_id') WHERE status = 'waiting_approval'`. Additive, hand-written, no table, column or enum, so no CHECK to review; downgrade tested |
| API | `GET /auth/me`; `GET /approvals` enriched by `modules/refunds/approvals_view.py` (`decided_by`, `decided_at`, `decision_reason`, `expires_at`, `refund` and `order` summaries, `review_reasons`, `review_text`, `requester_refunds_in_window`; ids only, no emails); `waiting_for` on `GET /workflows/{id}`; `GET /admin/knowledge/search` (`api/routers/admin.py`) |
| Staff lookup (`rag/audience.py`, `api/routers/admin.py`) | `audiences_for_role(role)`: admin gets `customer` and `internal`, everyone else `customer`. The endpoint calls the retriever directly (no model, no tool), refuses a customer before embedding, audits `admin.kb_search` without the question text, and holds no transaction while embedding. `retrieval_params` moved from the tool handler to `rag/factory.py` so both use it |
| Frontend | `app/admin/page.tsx` (tabs, cards, Approve and Reject with an optional note, the backend's refusals as they are, signed-in user, sign-out, "no access" for a non-admin, the staff lookup, refresh every 15 s, first 50 of a tab); `lib/auth.ts` (token store and helpers); `lib/api.ts` (`getMe`, `listApprovals`, `decideApproval`, `searchKnowledge`, `waiting_for`); the chat page polls immediately and then every 15 s and shows "Waiting for human approval" or "Approved, payout in progress."; `app/icon.svg` |
| Scripts | `scripts/demo_phase5.py` (16 checks); `scripts/mutation/phase5.json` (9 mutations) |

## Commits

Nine code commits since the last Phase 4 commit (`42ffd59`), then four documentation commits (this report is the last). Changes outside the docs, `42ffd59..e3b3781`: 33 files, +2908 / −105 lines (9 test files hold +1169 of them).

| Commit | What |
|---|---|
| `c3e579b` | Phase 5 (1/8): shared `finish_waiting` with the payout rule on the fallback too, `waiting_for` on `GET /workflows/{id}`, migration `0005`, tests (including reconcile finishing a waiting turn); mutation `A11`'s pattern updated |
| `429bfdd` | (2/8) the race closure; tests force the window |
| `185d3ec` | (3/8) the resume consumer; tests including two Redpanda-backed ones |
| `d40c277` | (4/8) `GET /auth/me` and the enriched approvals list |
| `e698260` | (5/8) the staff knowledge-base lookup and `audiences_for_role` |
| `5b1a653` | (6/8) the admin page, the shared auth helper, the 15 s poll and the waiting states |
| `c2620db` | (7/8) `scripts/demo_phase5.py` |
| `350e4b0` | (8/8) the `phase5` mutation list |
| `e3b3781` | `app/icon.svg`, so the favicon request stops returning 404 (found by the browser check) |
| `706871d`, `4236d57`, `2a56b01`, and this report | Documentation: README (and `CLAUDE.md`'s current phase), architecture, decisions 38–43 |

## Test summary

`pytest --collect-only -q` reports **1137** tests: **1107** not marked `kafka` and **30** marked. The whole suite (Postgres and Redpanda up) at `e3b3781`: **1136 passed, 1 skipped in 4 min 30 s** (the skipped test needs POSIX). That is 74 more than the 1063 counted in the Phase 4 report; the seven new test files hold 65 of them, and the other 9 are not attributed per file here. `ruff check` and `ruff format --check` are clean (209 files).

| File | Tests | What it covers |
|---|---:|---|
| `test_migration_0005.py` | 4 | The index is partial and on the checkpoint key; the planner can use it for the consumer's lookup; the downgrade drops it and the upgrade restores it; a linear revision chain |
| `test_resume_workflow.py` | 16 | `waiting_for` for every approval and refund combination; an unusable checkpoint counts as still waiting; a decided but unfinished payout keeps the turn waiting until it resolves; a payout the reconciler finds declined finishes the turn as failed; a rejection is final at once; finishing never calls the model or runs a tool; another user's refund in a checkpoint is never trusted; the workflow view's `waiting_for` |
| `test_resume_race.py` | 4 | An approval, a rejection, an approved-but-unpaid refund and no decision at all during the final answer |
| `test_resume_consumer.py` | 9 | A completed event finishes once and a redelivery adds nothing; rejected finishes; approved while processing waits for the payout; an event for a refund nobody waits for is claimed and changes nothing; another customer's event, an event type that decides nothing and a key that disagrees with the data are ignored; an unusable event is dead-lettered and leaves no claim; the refund is never touched |
| `test_resume_consumer_kafka.py` | 2 | **Redpanda-backed:** an approval travels through Redpanda and finishes the turn; a poison message is dead-lettered and the real event still resumes the turn |
| `test_admin_api.py` | 13 | `/auth/me` from the database and ignoring a forged role claim; a customer's `403` on the list; everything the page shows is in the list, with no email or name; decision history; the velocity count is per customer; a customer cannot decide; the assistant cannot decide even when an admin is chatting; self-approval refused and a second admin can decide |
| `test_staff_kb_search.py` | 17 | A customer's `403` before anything is embedded, a forged admin claim, `401` without a login; the audience cannot be chosen by the request; the mapping is one function the agent does not use; an admin finds internal documents, labelled; bad questions are `422`; every search is audited without the question; an outage is a generic `503`; no transaction is open while embedding |

Existing tests changed only where the facts changed (the allow-list in `test_no_direct_publish.py`, the `A11` pattern's test list). `conftest.py` still forces the fake LLM and fake embedder, so no test can reach a real model or the embedding API.

## Mutation testing

`python scripts/mutation/run.py --list` shows eight lists, **93** mutations (agent 18, cleanup 12, commit2 8, commit3 10, commit4 14, commit5 14, phase5 9, rag 8). Run once each, from a clean tree, today: the whole `phase5` list and `agent` A6 and A11. **11 of 11 caught**, each by the test that should catch it. No mutation survived and none was NOT APPLIED, so nothing had to be fixed or retargeted. The other lists were not re-run in Phase 5.

| Id | Defect | Result and the first failing test |
|---|---|---|
| P1 | the admin role check is gone from `GET /approvals` | caught: `test_a_customer_gets_403_and_nothing_from_the_approvals_list` |
| P2 | the role check is gone from the staff lookup | caught: `test_a_customer_gets_403_before_anything_is_embedded_or_searched` |
| P3 | `audiences_for_role` gives internal documents to every role | caught: `test_the_role_to_audience_mapping_is_one_function_and_the_agent_does_not_use_it` |
| P4 | a turn is finished while its approved refund is still processing | caught: `test_a_decided_but_unfinished_payout_keeps_the_turn_waiting_until_it_resolves` |
| P5 | the race closure is removed | caught: `test_an_approval_decided_during_the_final_answer_still_finishes_the_turn` |
| P6 | a checkpoint naming another user's refund is trusted | caught: `test_a_checkpoint_that_names_another_users_refund_is_never_trusted` |
| P7 | the consumer ignores whom the event is about | caught: `test_an_event_about_another_customer_never_finishes_the_turn` |
| P8 | the consumer reacts to events that decide nothing | caught: `test_an_event_type_that_decides_nothing_is_ignored` |
| P9 | the staff lookup embeds with a transaction still open | caught: `test_no_transaction_is_open_while_the_question_is_embedded` |
| A6 | a repeated `message_id` no longer replays the stored response | caught: `test_the_same_message_id_twice_is_a_replay_with_one_refund_and_no_new_model_call` |
| A11 | `finish_waiting` finishes an already-finished workflow again | caught: `test_sync_finishes_a_waiting_workflow_once_and_only_once` |

`A11` (`agent.json`) had to be edited in commit `c3e579b` because the code it targets moved from the sync stub into `finish_waiting`: its description and test list were updated, its pattern still applies, and it was caught again today. Its test list was extended with `test_resume_workflow.py`; no test was weakened.

## The demo and the real-browser check

**`scripts/demo_phase5.py`** (fake LLM and fake embedder forced for the processes it starts, whatever `.env` says; a private topic prefix and knowledge-base namespace per run; refuses to run unless `APP_ENV=dev`, or if a relay is already running): (a) a customer's 700 refund waits; (b) the admin's `/auth/me` and enriched list, while a customer gets `403`; (c) the admin approves and, **before any `GET /workflows`, the script reads the database**: the consumer has already finished the turn; (d) the same event delivered again changes nothing; (e) the consumer is stopped by PID, a second refund is approved, and the turn stays waiting until the customer's next GET, when the fallback finishes it; (f) self-approval is refused; (g) the staff lookup finds an internal document. **16/16 checks, run twice, identical `OUTCOMES` line**, 15 s for the first run. **The demo's relay publishes every unpublished row of the dev outbox table, including rows left by earlier dev activity, to its private prefix and marks them published.** Run 1 started with 0 such rows and run 2 with 2: the 2 were the last events of run 1 (the self-approval request and rejection), which its relay had not yet published when it was stopped. 2 unpublished rows are again left after run 2. Port 8115 was used.

**Real Edge (headless, version 155), driven over the DevTools protocol by scripts that are not in the repository** (`stack.py` starts the stack and `drive.mjs` drives the tabs, both in a scratch directory), against private copies: the API on 8116, the page on 3116 (`next dev`, proxying to 8116) and Edge's debug port 9336, with the fake LLM and fake embedder, and the relay and resume consumer started by PID on a private topic prefix. The owner's servers on 3000 and 8000 were not touched. What I saw:

1. A customer's "my order … arrived damaged, refund 700" showed `list_orders succeeded READ_ALLOWED`, `request_refund awaiting_approval REFUND_ABOVE_AUTO_LIMIT` and "⏳ Waiting for human approval."
2. In a second tab, the admin page (admin login) showed "Signed in as demo-admin-… (admin)", the four tabs, and the request with `REFUND_ABOVE_AUTO_LIMIT` and its text ("A refund of INR 700.00 is above the automatic limit of INR 500.00 and needs approval"), the order and the requester's refund count.
3. The **Expired tab showed "Nothing expired."** Nothing on the page claims that approvals expire (the cards do not show `expires_at`).
4. After Approve, the customer's tab changed to `COMPLETED` with "Update: your refund of 700.00 INR was approved and has been paid." **without a reload** (a marker set in the page survived). The consumer logged `workflow_resumed` at 06:28:08.755, **107 ms** after the approve request (06:28:08.648), and the page's own `GET /workflows/…` came at 06:28:19, about 10 s later: **the event finished the turn, not the fallback.**
5. A customer who signed in and then opened `/admin` saw "You do not have access to this page." and the page made **one** request, `GET /api/v1/auth/me`. The same customer's token got `403` on `GET /approvals` and on the staff lookup.
6. **0 console errors, 0 warnings and 0 HTTP 4xx/5xx** in all three tabs (the page was served by `next dev`).

## Verification: who saw what

| Claim | Status |
|---|---|
| Everything under "How this was verified" below | **Run by me** on 2026-10-12, with the commands shown |
| The Edge check above | **Seen by me**; the driver scripts are **not in the repository** |
| The whole Phase 5 flow in a real browser | **Reported by the project owner, not seen by me:** they checked it in their own browser around 10 October 2026 and it worked. I have no further details |
| Behaviour with the real Gemini models in Phase 5 | **Not exercised by me.** Everything above uses the fake model and fake embedder; the backend's decisions do not depend on the model |
| The 15 s poll against a slow real model | **Unverified**; the page's poll is independent of the model |

## Problems found along the way

1. **The decide-before-waiting race.** A turn becomes `waiting_approval` only when it commits its final answer, which takes seconds with a model. An admin who decides in that window produces an event that finds no waiting workflow, so the customer would never be told. Fixed in `429bfdd` (one `finish_waiting` after the commit), with tests that force the window and mutation `P5`.
2. **Approved, but the payout is still processing.** Finishing the turn on the approval alone would say "approved" with no outcome. Fixed in `c3e579b`: `waiting_for` returns `"payout"`, both the consumer and the fallback stay waiting, and the later `refund.completed` / `refund.failed` event (also emitted by the reconcile path, tested) finishes it; mutation `P4`.
3. **Favicon 404.** The browser check showed a failing `favicon.ico` request. Fixed in `e3b3781` with `app/icon.svg`.
4. **Mutation `A11` had to be updated** for the refactor that moved the logic into `finish_waiting` (`c3e579b`); it applies and is caught.
5. **My first Edge run had a driver bug.** It clicked the "Approved" tab instead of the Approve button, because the click helper matched the start of a label and "Approved" comes first in the page. Nothing was decided (the database showed no new decision) and the run was repeated on fresh demo users with a driver that clicks the exact button in the right card.
6. **The default `node` here is v18.17**, and `tsc` fails under it (`ERR_UNKNOWN_FILE_EXTENSION`). The typecheck and build were run with Node 24.15 put first on `PATH` for those commands only.
7. **The Pending tab lists about 14 older pending approvals** left in the dev database by earlier runs (one requester has several; they are oldest first, so they come before a new request). I did not touch them.
8. **Each demo run leaves 2 unpublished outbox rows**, and the next run's relay publishes them to its own private prefix (see the demo section).

## Deliberately not included

- **Approval expiry** (deferred, decision 43): `expires_at` exists and is returned, nothing sets or enforces it, and the Expired tab is always empty.
- Admin access to customers' chat transcripts (conversations and workflows stay owner-only, administrators included), login rate limiting, token revocation, Redis rate limiting.
- Any change to the executor, the policy engine, the idempotency code or `rules.yaml`. Checked with `git diff 42ffd59..HEAD` over `backend/app/tools/executor.py`, `backend/app/policy/` and `backend/app/modules/refunds/` (only the new `approvals_view.py` is added there; `service.py`, `repo.py` and `hashing.py` are unchanged). `rules.yaml` is under `backend/app/policy/`.
- A health check or alert for the resume consumer (Phase 6).
- Frontend tests in the repository (only `tsc`, `next build` and the out-of-repo Edge check guard the pages).

## Open items

1. **After Approve or Reject the card leaves the Pending tab with its confirmation**, so a decision is only visible by opening the Approved or Rejected tab. **Not fixed.**
2. **The fallback can hide a dead consumer.** Nothing alerts on a stopped consumer or relay yet (Phase 6); `workflow_resumed` log lines are the only signal.
3. **The Pending tab shows everything pending, oldest first, up to 50**, with no filtering or paging.
4. **Approval expiry is deferred** (decision 43); a request nobody decides waits forever.
5. **Leftover rows in the dev database:** the `demo-*` users, orders and approvals from the two demo runs and the browser check (including one pending 700 refund left by the aborted first browser run), the demo knowledge-base namespaces, and 2 unpublished outbox rows. `scripts/demo_cleanup.py --apply` and `scripts/demo_phase4.py --cleanup --apply` would remove them; **neither has been run** (both are dry runs by default).
6. **The Edge scripts** (`stack.py`, `drive.mjs`) are not in the repository.
7. **The 9 tests beyond the seven new files** are not attributed per file.
8. **`docs/events.md`** was written in Phase 2 and does not describe the resume consumer; `architecture.md` and decision 38 do.
9. **Carried over from Phase 4:** the real-model behaviours (refund reason, offering to cancel an unpaid order) remain unverified with the real model; the retrieval thresholds were tuned on the questions that measure them.

## How this was verified

| Check | Command or evidence | Result |
|---|---|---|
| Lint | `ruff check . ../scripts` and `ruff format --check . ../scripts` (from `backend/`) | clean (209 files formatted) |
| Frontend | `npm run typecheck`, `npm run build` (Node 24.15 first on `PATH`; the default `node` is 18.17 and fails) | pass; routes `/`, `/_not-found`, `/admin`, `/icon.svg` |
| Test count | `pytest --collect-only -q` (from `backend/`) | 1137 collected; `-m kafka` 30, the rest 1107 |
| Whole suite | `pytest` (Postgres and Redpanda up) at `e3b3781` | 1136 passed, 1 skipped, 4 min 30 s |
| Mutation | `python scripts/mutation/run.py phase5`; `run.py agent --only A6,A11` | 9 of 9 and 2 of 2 caught; 8 lists and 93 mutations in `--list` |
| Demo | `scripts/demo_phase5.py --port 8115`, twice | 16/16 both times, identical `OUTCOMES` |
| Real Edge | headless Edge 155 over CDP, private ports 8116, 3116 and 9336, fake LLM and embedder | the flow in the section above; 0 console errors |
| Protected code unchanged | `git diff --stat 42ffd59 HEAD` over the executor, `policy/`, `modules/refunds/` | only `modules/refunds/approvals_view.py` (new) |
| ER diagram | regenerated from `Base.metadata` and compared with the committed block | same 15 tables, columns and foreign keys (no schema change in Phase 5); not rewritten |
| Mermaid | the real `mermaid` package (12.1.0, installed in a scratch directory outside the repository) over `architecture.md` | 11 diagrams parse; a deliberately broken one fails the same check |
| Module tree | generated from `git ls-files` and compared with the committed tree | the only differences were the seven new files, now included |
| Links | a script that mimics GitHub's heading slugs, over `README.md`, `CLAUDE.md` and every file in `docs/` | 133 relative links and `#anchors` checked, 0 problems |
| Owner's browser session | **reported, not seen** | see "Verification: who saw what" |
