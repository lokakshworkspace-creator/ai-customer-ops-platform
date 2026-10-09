# Phase 4 report

Status report for Phase 4: RAG. The assistant answers policy and FAQ questions from a knowledge base, cites the articles it retrieved, and says "I'm not sure" when nothing relevant is found; documents **inform but never authorize**. Figures were measured on 2026-10-09 on the development machine (Windows 11, Python 3.13, Node 24, Docker containers for Postgres, Redis and Redpanda) against the code at commit `887848b` (the commits after it are documentation). Where something could not be verified it is marked **unverified**, and who ran what is stated.

- [Summary](#summary)
- [What was built](#what-was-built)
- [Commits](#commits)
- [Test summary](#test-summary)
- [Mutation testing](#mutation-testing)
- [Retrieval quality: the two final eval printouts](#retrieval-quality-the-two-final-eval-printouts)
- [The models: what was verified and what was not](#the-models-what-was-verified-and-what-was-not)
- [The demo and the UI](#the-demo-and-the-ui)
- [Problems found along the way](#problems-found-along-the-way)
- [Deliberately not included](#deliberately-not-included)
- [Open items](#open-items)
- [How this was verified](#how-this-was-verified)

## Summary

- **Documents inform; they never authorize.** `search_knowledge_base` is a read-only tool behind the **same executor** (validate → authorize → policy → execute → log); the executor, the policy engine and the idempotency code did not change. A retrieved chunk is data: the policy, refund and executor code does not import the knowledge base (a test scans the source), and the tests show that a lying article and a poisoned one, really retrieved by a gullible model, change no decision.
- **The audience filter is in the SQL**, in every candidate query and the final select. The agent searches `customer` documents only, **even when an administrator is chatting**; `audience`, `namespace` and `top_k` are not tool fields. Internal text reached neither a customer nor an admin in any test, in the demo, or (as reported by the owner) in their own browser.
- **"I'm not sure" is a real outcome.** Retrieval is hybrid (vector + full text, fused with Reciprocal Rank Fusion), but a chunk is returned only if it clears an **absolute** relevance threshold, because RRF only ranks.
- **Sources are computed by the backend** from the search result and returned in `/chat`; the UI shows them as "Sources consulted:" chips. They are the articles that were retrieved, **not necessarily the ones the answer used**.
- **Real embeddings, no new dependency.** Gemini embeddings over plain `httpx` (`gemini-embedding-001` and `gemini-embedding-2` were both evaluated), a deterministic fake embedder for **all tests and the demo**, a hard-coded `vector(768)` column with loud dimension and model mismatch errors.
- **Honest numbers.** On 25 golden questions **hybrid did not beat vector-only**; the **thresholds were tuned on the same 30 questions** that measure them; differences of one or two questions out of 25 are noise. The printouts are below, verbatim.
- **15 tables** (two new), **1063 tests** (1035 fast, 28 need Redpanda; 189 more than at the end of Phase 3), **84 mutation checks** in seven lists (the new `rag` list: 8, all caught), a repeatable demo (21 checks) and source chips in the page.
- Not done on purpose: internal documents for admins (Phase 5), a re-ranker, answer-level evaluation, streaming, an out-of-sample question set.

## What was built

| Area | What exists |
|---|---|
| Knowledge base (`backend/app/data/kb/`) | 10 markdown documents with YAML front matter: 8 `customer` (shipping, returns, refunds, cancellations, payments, warranty, damaged items, contact and support) and 2 `internal` (an approvals playbook with escalation codes, fraud notes). Numbers that come from the rules are placeholders rendered from `rules.yaml`; a consistency check fails the load on any other number |
| Chunker (`rag/chunker.py`) | Heading-aware (`#`–`###`), heading path kept with each chunk, about 300 estimated tokens, overlap on split sections, stub sections merged: 23 chunks in all |
| Embedders (`rag/embedder.py`, `factory.py`) | `Embedder` interface; `GeminiEmbedder` (REST `batchEmbedContents`, one text per item, 768 dimensions, task type for `-001` and a text prefix for `-2`, L2-normalised, paced and retried, `SecretStr` key); `FakeEmbedder` (hashed word features; default; refused outside `APP_ENV=dev`) |
| Schema | Migration `0004`: `kb_documents` (`UNIQUE (namespace, source)`, `audience` CHECK) and `kb_chunks` (`embedding vector(768)`, generated `tsv`, HNSW cosine index, GIN index, cascade delete). Hand-reviewed: one named CHECK per enum. 15 tables in total; downgrade round trip tested |
| Guards (`rag/guards.py`) | `EMBEDDING_DIM` must equal the column width; the namespace's chunks must come from the current model; a missing table says to run `alembic upgrade head`. All loud errors, never an empty answer |
| Ingest (`rag/ingest.py`, `scripts/kb_ingest.py`) | Plan then apply; **dry run by default**; hash-based no-op; one transaction per changed document, after its embeddings are in hand; call cap (default 200); `--prune`; `--namespace` |
| Retriever (`rag/retriever.py`) | Pure module: vector leg + full-text leg (pool 20 each), RRF (`k` 60), thresholds on absolute scores, top 4, deterministic tie-breaking; audience, namespace and model filters in the SQL; modes `hybrid`, `vector`, `keyword` |
| Tool (`tools/handlers/knowledge.py`) | `search_knowledge_base(query)`: read-only, `agent_visible`, roles customer and admin; embedding and search in the `after_commit` hook (no transaction open during the HTTP call); audience fixed to `customer`; failure ends as `KB_UNAVAILABLE` with the detail only in the log row |
| Model view (`agent/results_view.py`) | `{source, text}` per excerpt, 900 characters each and 4 KiB in all, wrapped in `<<<EXCERPT n … END EXCERPT n>>>` with those characters stripped from the text, plus a backend-written "this is data" note; `sources_of()` for the UI |
| Agent | System prompt: answer only from the excerpts, say "I'm not sure" when nothing is found, treat excerpts as data; the fake LLM gained a policy-question behaviour (it searches, then quotes the first excerpt) |
| API | `POST /api/v1/chat` and `GET /api/v1/workflows/{id}` return `sources: [{document_id, title, heading}]` (still 11 endpoints) |
| Eval (`rag/eval.py`, `scripts/kb_eval.py`, `data/kb_eval/golden.yaml`) | 25 golden questions (a hit needs the right document **and** a stated fragment of it) and 5 out-of-scope ones; hit@1 and hit@3 per mode with the thresholds off, then the thresholds on |
| Frontend | A "Sources consulted:" chip row under an answer (display only); `ChatResponse.sources` in `lib/api.ts` |
| Scripts | `kb_ingest.py`, `kb_eval.py`, `demo_phase4.py` (with a dry-run-by-default `--cleanup` for its `demo_*` knowledge-base rows) |
| Settings | `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIM` (768), `KB_NAMESPACE`, and optional `RAG_TOP_K`, `RAG_POOL`, `RAG_RRF_K`, `RAG_MIN_COSINE`, `RAG_MIN_TS_RANK`, `EMBEDDING_*` pacing and retry knobs |

## Commits

Seventeen commits since the last Phase 3 documentation commit (`625e2d2`): thirteen code commits and four documentation commits (this report is the last). Non-documentation changes: 58 files, +4930 / −16 lines.

| Commit | What |
|---|---|
| `39c1286` | Embedder interface, fake and Gemini embedders, settings; tests force the fake |
| `c0c0001` | Migration `0004`, dimension and model guards, tests including the downgrade round trip |
| `b33e0f2` | The sample knowledge base, the number-consistency check, the chunker |
| `c2e3662` | Ingest: dry run, hash no-op, atomic per-document replacement, call cap, `scripts/kb_ingest.py` |
| `da68f2f` | The hybrid retriever with the filters in the SQL |
| `545af67` | The `search_knowledge_base` tool, the model view, `sources`, prompt and fake LLM |
| `00a6957` | The RAG safety tests and the `rag` mutation list |
| `f6b4fa5` | Mutation `K6` retargeted (it was an equivalent mutant) and a test for what it now checks |
| `f9ee2e1` | The golden questions, the eval harness and `scripts/kb_eval.py`; keyword threshold calibrated |
| `394054a` | A missing table is a clear error, not a raw SQL error |
| `2b3a740` | Deterministic tie-breaking; per-model cosine thresholds for the Gemini embedders |
| `8fe78d5` | Source chips in the UI; `scripts/demo_phase4.py` |
| `887848b` | The chip label "Sources consulted:" |
| `e83eb09`, `34ced42`, `392d0b1`, and this report | Documentation: README, architecture (retrieval section, regenerated ER diagram and module tree), decisions 31–37, this report |

## Test summary

`pytest --collect-only -q` reports **1063** tests: **1035** not marked `kafka` and **28** marked. The whole suite (Postgres and Redpanda up) at `8fe78d5`: **1062 passed, 1 skipped in 3 min 30 s** (the skipped test needs POSIX). After that only a UI label changed; the frontend `typecheck` and `build` were re-run. `ruff check` and `ruff format --check` pass.

| File | Tests | What it covers |
|---|---|---|
| `test_rag_embedder.py` | 28 | Fake determinism and normalisation; the Gemini request shapes per model, batching, retry and backoff, error mapping, the key never in a URL, log or error |
| `test_migration_0004.py` | 12 | Columns, constraints, HNSW and GIN indexes, one CHECK per enum, the dimension and model guards, a missing table, the downgrade round trip |
| `test_rag_kb.py` | 28 | Placeholder rendering from `rules.yaml`, the number-consistency check, the chunker |
| `test_rag_ingest.py` | 15 | Dry run writes nothing, hash no-op, atomic replacement when embedding fails, call cap, pruning, guards, the real CLI as a process |
| `test_rag_retriever.py` | 30 | Right document per mode, scores, RRF, audience isolation in all modes, namespace and model isolation, thresholds and out-of-scope, hostile queries, reproducible order after a re-ingest |
| `test_rag_tool.py` | 29 | The tool as an ordinary logged read; extra arguments rejected; internal text never for customer or admin; failures generic; no transaction open while embedding; the model view (allow-list, delimiters, cap); sources; the chat flow |
| `test_rag_safety.py` | 15 | Internal text through `/chat` for customer and admin, at SQL level for every internal chunk; a poisoned and a lying article change nothing; policy code cannot read documents |
| `test_rag_eval.py` | 30 | The golden file is well formed and answerable, hit definition, the harness end to end |

The eight new files hold 187 of the 189 added tests. Existing tests changed only where the facts changed (the agent tool set, the `sources` key in the chat response, the Gemini tool-declaration golden snapshot regenerated, the table count 15). **With the fake embedder the RAG tests check plumbing, the audience filter, thresholds, atomic ingest and that the five out-of-scope questions come back "not found"; none of them claims anything about real retrieval quality.** `conftest.py` forces `EMBEDDING_PROVIDER=fake`, so no test can reach the embedding API.

## Mutation testing

`python scripts/mutation/run.py --list` shows seven lists, **84** mutations (agent 18, cleanup 12, commit2 8, commit3 10, commit4 14, commit5 14, **rag 8**). In Phase 4 only the new `rag` list was run, once, from a clean tree: **8 of 8 caught**, after one correction.

| Id | Defect | Result |
|---|---|---|
| K1 | the audience filter is gone from the SQL | caught |
| K2 | the agent's audience list includes internal documents | caught |
| K3 | relevance thresholds are ignored | caught |
| K4 | vectors of other embedding models are compared | caught |
| K5 | the delimiter characters are kept in excerpts | caught |
| K6 | a document found by two searches in one turn is listed twice as a source | caught |
| K7 | the model view is not size-capped | caught |
| K8 | a wrong `EMBEDDING_DIM` is not noticed | caught |

**K6 was an equivalent mutant that was retargeted.** In its first form it changed the de-duplication inside `sources_of()`, which survived the run: that function already de-duplicates by dictionary key, so the mutation changed nothing observable. It was retargeted at the de-duplication that matters, across several searches in one turn (`add_sources` in the orchestrator), a test for that case was added (`test_a_document_found_by_two_searches_in_one_turn_is_cited_once`), and the retargeted mutation was caught. The `agent` list's `A8` pattern was edited because the line it targets changed; **the other lists were not re-run in Phase 4** (the full test suite passed, which is a weaker statement).

## Retrieval quality: the two final eval printouts

How to read them, before the numbers:

* **The thresholds were tuned on the same 30 questions that measure them.** The cosine thresholds (0.58 and 0.64) and the keyword threshold (0.12) sit halfway between the lowest best score of an in-scope question and the highest best score of an out-of-scope one. So "25/25 answered" and "5/5 not found" are **not** an out-of-sample result. Before tuning, at a cosine of 0.55, the real runs wrongly answered one out-of-scope question with `gemini-embedding-001` (4/5 correct) and two with `gemini-embedding-2` (3/5).
* **Hybrid did not beat vector-only.** For both models hybrid ties or trails vector at hit@1 and hit@3. It was kept because it is what was designed and approved and because the keyword leg does not depend on the embedder, not because these numbers favour it.
* **Differences of one or two questions out of 25 are noise.** Thirty questions cannot separate two good systems, and the gap between in-scope and out-of-scope scores is only about 0.02 cosine.
* A first real run showed `gemini-embedding-001` hybrid at 22/25 at hit@1. That was an artefact: equal keyword ranks were tied arbitrarily, and the lucky ordering put the right chunk first. Ties are now broken by document and chunk position and both models were re-run; the printouts below are the re-runs.
* Nothing is claimed from the fake embedder.

**`gemini-embedding-001`** (namespace `eval_g001`, run for this report; the same numbers were reproduced later against the `default` namespace):

```text
embedding model : gemini-embedding-001    namespace: eval_g001
questions       : 25 in scope, 5 out of scope

mode        hit@1   hit@3   missed in top 3
vector      21/25   24/25   q18
keyword     19/25   24/25   q09
hybrid      20/25   24/25   q09

at the configured thresholds (min_cosine=0.58, min_ts_rank=0.12):
  in-scope questions answered        : 25/25
  out-of-scope correctly 'not found' : 5/5

raw scores the thresholds are calibrated from:
  cosine  : lowest best-score of an in-scope question 0.589, highest best-score of an out-of-scope one 0.567
  ts_rank : lowest 0.167 in scope, highest 0.091 out of scope
```

**`gemini-embedding-2`** (namespace `eval_g002`):

```text
embedding model : gemini-embedding-2    namespace: eval_g002
questions       : 25 in scope, 5 out of scope

mode        hit@1   hit@3   missed in top 3
vector      23/25   24/25   q18
keyword     19/25   24/25   q09
hybrid      20/25   23/25   q09, q18

at the configured thresholds (min_cosine=0.64, min_ts_rank=0.12):
  in-scope questions answered        : 25/25
  out-of-scope correctly 'not found' : 5/5

raw scores the thresholds are calibrated from:
  cosine  : lowest best-score of an in-scope question 0.656, highest best-score of an out-of-scope one 0.625
  ts_rank : lowest 0.167 in scope, highest 0.091 out of scope
```

The `eval_g001` and `eval_g002` namespaces were deleted afterwards, by namespace and at the owner's request, so the `gemini-embedding-2` printout can only be reproduced by ingesting into a new namespace with `EMBEDDING_MODEL=gemini-embedding-2` and running `scripts/kb_eval.py --namespace NAME`. The `default` namespace holds the `gemini-embedding-001` index (10 documents, 23 chunks), on which the first printout was reproduced exactly.

## The models: what was verified and what was not

| Claim | Status |
|---|---|
| Both Gemini embedding models work with the owner's free-tier key at 768 dimensions through `batchEmbedContents` | **Verified by me** in Phase 4 before the design was fixed: the request shapes, the per-model task handling, that `-001` returns unnormalised vectors at 768 (so they are normalised here), that a single item with several parts aggregates into one vector (hence one text per item); about 12 sequential calls were fine. **The free-tier limits are not published**, so they are not stated |
| The ingest of 10 documents (23 chunks, 10 calls) and the eval (about 30 calls per run) with both models | **Run by me**, several times, without rate-limit errors; pacing (0.3 s between calls) and retry with backoff are in the client and covered by mock-transport tests |
| Quality of retrieval | **Measured by me** with the printouts above, with the caveats above. The effect of using 768 dimensions instead of the models' native width was **not measured** |
| The key never appears in a URL, log, error or test output | Covered by tests; **I did not read `.env`** and the key was never printed |
| The chat page with the real chat model and real embeddings | **Reported by the project owner, not seen by me.** In their own browser, with `gemini-3.1-flash-lite` and `gemini-embedding-001` on the `default` namespace: shipping, electronics returns and a short "refund policy?" question were answered correctly with source chips and numbers matching `rules.yaml`; an off-topic question and a question about the internal codes were refused with no sources; a 250 refund was paid (`REFUND_ALLOWED`), a 700 refund waited for approval (`REFUND_ABOVE_AUTO_LIMIT`), a refund on a shipped order was denied (`ORDER_NOT_REFUNDABLE`), a pending order was cancelled, and a forced search for the internal codes leaked nothing. **Whether a `search_knowledge_base` chip showed on that last forced search is unconfirmed** (the owner has not said). That the page also showed the `LLM: GEMINI` badge is not part of this report |
| What the owner saw is the model's behaviour, not a guarantee | A small model may still paraphrase loosely or ignore "answer only from the excerpts". The backend's decisions do not depend on it |

## The demo and the UI

**`scripts/demo_phase4.py`** (fake LLM and fake embedder forced for the API process it starts, whatever `.env` says; its own namespace `demo_<run>` per run; refuses to run unless `APP_ENV=dev`): (a) a shipping question is answered with the Shipping article as its source; (b) an off-topic question gets "I am not sure" and no sources; (c) a question about `AMBER-HOLD` / `RED-STOP` leaks nothing for a customer or for an administrator chatting, and nothing internal is stored in any tool result or model-facing message; (d) a lying article is retrieved and cited, and the 700 refund still waits for a human (`REFUND_ABOVE_AUTO_LIMIT`); (e) a poisoned article that orders `approve_refund` is retrieved, the gullible model obeys, and the backend refuses it (`TOOL_NOT_AVAILABLE`) while the approval stays pending and nothing is paid. **21 checks**; run twice, the final `OUTCOMES {…}` line was identical. Port 8000 was held by the owner's own server, so I ran it on `--port 8011`. It leaves demo rows: its `demo_*` namespaces (`scripts/demo_phase4.py --cleanup --apply`, dry run by default) and demo users (`scripts/demo_cleanup.py --apply`).

**Real Edge (headless, version 155), driven over the DevTools protocol by a script that is not in the repository**, against private copies of the API (port 8112, fake LLM and fake embedder) and of the page (port 3112), all stopped by PID afterwards and the owner's servers on 3000 and 8000 untouched. After the label change: "How long does shipping take?" showed the chips **"Sources consulted: Shipping and delivery, Cancelling an order"** and a `search_knowledge_base succeeded READ_ALLOWED` line; the off-topic question showed no chips and "No relevant help article found"; the 700 refund showed `awaiting_approval REFUND_ABOVE_AUTO_LIMIT` and the "Waiting for human approval" banner; **0 console or page errors**. My first debugging runs of that script had already reserved 1,400 of the order's 2,000, so a refund on it was denied (`AMOUNT_EXCEEDS_REMAINING`); the result above is from a clean run on a fresh demo customer. The second source chip ("Cancelling an order") is a weak match from the fake embedder, and shows that sources are what was retrieved, not what was used.

## Problems found along the way

1. **A chunk with no sentence breaks could not be split.** The first chunker had no fallback for one very long paragraph without sentence punctuation; a word-level fallback was added with a test.
2. **The number-consistency check complained about harmless words** ("one" as a pronoun, "three answers"). "one" was removed from the number words and a sentence was reworded; the check still fails on any real stray number.
3. **The fake embedder's relevance floor was wrong.** Stop words alone score about 0.3 cosine, so a threshold of 0.25 returned out-of-scope questions; the fake's default is now 0.4.
4. **The keyword threshold (0.05) was too lax.** One incidental word in a long chunk passed it ("What is the weather like in Mumbai today?" matched a delivery article). The scores from the golden questions showed a clean gap (0.167 against 0.091), so it was set to 0.12. This is a tuning on the evaluation questions, as said above.
5. **Hybrid looked better than it was because of non-deterministic ties** (see the printouts section). Fixed, re-run, and a test pins that a ranking is identical after a re-ingest with new random chunk ids.
6. **An equivalent mutant** (K6), described under mutation testing.
7. **A commit went in while `ruff` was failing.** A shell pipeline (`ruff … | grep`) hid the exit status; the lint was fixed and the unpushed local commit amended. Later commits were gated on `ruff`'s own exit status.
8. **A test fixture trap.** `apply_plan` ends its read transaction with a rollback (so none is open during embedding calls); inside the rolled-back-per-test session that silently undid a test world created just before it. The RAG fixtures load the knowledge base first, then the world.
9. **A raw SQL error for a missing table.** Running the ingest against the dev database before `alembic upgrade head` raised an asyncpg error; the guard now says to run the migration, with a test. The dev database was then upgraded to `0004` (additive).
10. **Expected fallout of adding a tool.** The agent tool set, the chat response keys, the Gemini declarations snapshot and one mutation pattern had to be updated deliberately.
11. **Shell and tooling.** Heredocs with apostrophes and escapes were repeatedly mangled by the shell in this environment, a line-number-based text replacement once overwrote the wrong lines (repaired with exact-text edits), and one PowerShell `Copy-Item … -Force` was blocked by the harness as a removal; edits were made with exact-string tools or script files instead.

## Deliberately not included

- Internal documents for administrators (the audience stays in the schema, ingest and retriever for the Phase 5 admin UI).
- A re-ranker, query rewriting, chunk-level access control beyond the two audiences, incremental refresh on a rule change (re-run ingest), a document-upload API.
- An answer-level eval (is the final answer correct?) and an out-of-sample question set (Phase 6).
- Streaming; the admin approvals page and event-driven resume (Phase 5); observability.
- `sentence-transformers` (named in `CLAUDE.md`'s stack): the approved plan used the Gemini embedding API over `httpx` instead, with no new dependency; `CLAUDE.md` was not edited.

## Open items

1. **Refund reason.** With the real model, the assistant sometimes picks a refund reason itself instead of asking the customer (reported by the owner; I did not see it). The backend checks the reason either way (an unlisted reason is invalid, and the reason decides whether a refund is auto-approved), but the prompt could ask when the customer did not say.
2. **Pending order.** For a pending (unpaid) order the assistant offered to open a ticket instead of cancelling it (reported by the owner; I did not see it). `cancel_order` allows exactly this case; the prompt or the tool description is not steering the model to it.
3. **Unconfirmed:** whether a `search_knowledge_base` chip showed on the owner's forced search for the internal codes.
4. **Thresholds and hybrid.** Tuned on the evaluation questions, a gap of about 0.02 cosine, and hybrid not shown to beat vector-only: re-check with a larger, separate question set before trusting either. Vector-only is a one-word change (`mode`).
5. **Sources are the retrieved articles**, not necessarily the ones the answer used; a smarter attribution is not built.
6. **768 dimensions** (below the models' native width) was not compared with the native size.
7. **Mutation lists** `commit2`–`commit5`, `cleanup` and (except for one pattern edit) `agent` were not re-run in Phase 4.
8. **Leftovers in the dev database:** the `demo_69d04ed7` namespace and the demo users created by the demo and the browser check (`demo-customer-…`, `demo-admin-…`); the commands to remove them are in the demo section. The `default` namespace holds the real `gemini-embedding-001` index, which is meant to stay.
9. **`CLAUDE.md`'s "Current phase" line is stale** (it still says Phases 0–2 are done and Phase 3 is next). I did not edit it; say if you want it updated.
10. **The Edge scripts** used for the browser checks are not in the repository.
11. **Admin access to internal documents**, and an admin UI to see what they are reading, belong to Phase 5.

## How this was verified

| Check | Command or evidence | Result |
|---|---|---|
| Test count | `pytest --collect-only -q` (from `backend/`) | 1063 collected; `-m kafka` 28, `-m "not kafka"` 1035 |
| Whole suite | `pytest` at `8fe78d5` | 1062 passed, 1 skipped, 3 min 30 s |
| Lint | `ruff check . ../scripts` and `ruff format --check . ../scripts` (from `backend/`) | clean (194 files formatted) |
| Mutation | `python scripts/mutation/run.py --list`; `run.py rag` | 7 lists, 84 mutations; `rag` 8 of 8 caught (K6 after retargeting) |
| Eval, `gemini-embedding-001` | `scripts/kb_eval.py` against `eval_g001`, reproduced against `default` | identical printouts (above) |
| Eval, `gemini-embedding-2` | `scripts/kb_eval.py` against `eval_g002` | printout above; namespace since deleted |
| Demo | `scripts/demo_phase4.py --port 8011`, twice | 21/21 checks, identical `OUTCOMES` line |
| Frontend | `npm run typecheck`, `npm run build` | pass |
| Real Edge | headless Edge 155 over CDP, private ports, fake LLM and embedder | chips on a policy answer, none off-topic, the 700 refund waits, 0 console errors |
| Dev database | `alembic upgrade head` (0003 → 0004); `kb_ingest.py` dry run then `--apply` into `default`; four namespaces deleted by name | `default`: 10 documents, 23 chunks; `eval_g001` (10/23), `eval_g002` (10/23), `demo_b22cdaf8` (12/25) and `demo_ec5f127a` (12/25) deleted; `demo_69d04ed7` (10/23) left |
| Not verified by me | the owner's browser session with the real models | reported by the owner; see "The models" |
