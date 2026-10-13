# Architecture

This document describes the system as designed (the target) and as built (Phases 0–6). Anything not yet built is marked **planned**. Diagrams use [Mermaid](https://mermaid.js.org/), which GitHub renders natively.

- [Target architecture](#target-architecture)
- [The tool pipeline](#the-tool-pipeline)
- [The agent](#the-agent)
- [Retrieval: the knowledge base](#retrieval-the-knowledge-base)
- [Human approval: the admin page and event-driven resume](#human-approval-the-admin-page-and-event-driven-resume)
- [Hardening: login limiter, revocation, expiry, observability](#hardening-login-limiter-revocation-expiry-observability)
- [Request flows as implemented today](#request-flows-as-implemented-today)
- [Concurrency and locking](#concurrency-and-locking)
- [Data model](#data-model)
- [Golden rules](#golden-rules)
- [Module layout](#module-layout)

## Target architecture

A **modular monolith**: one FastAPI process, with orders, refunds and tickets as modules that each have a service layer and a repository layer. Postgres is the source of truth. The LLM only ever *proposes* tool calls; every proposal goes through schema validation, authorisation and the policy engine before anything executes. Four more processes run beside the API: the **outbox relay**, the **notification consumer**, the **workflow-resume consumer** and the **sweeper** (expires stale approvals, purges old revocations).

```mermaid
flowchart LR
    classDef built fill:#d4edda,stroke:#2e7d32,color:#111
    classDef prov fill:#fff3cd,stroke:#b8860b,color:#111
    classDef planned fill:#eeeeee,stroke:#777,stroke-dasharray: 5 3,color:#111

    UI["Web UI: Next.js chat page and admin page /admin, no business logic"]:::built
    HEALTH["GET /health"]:::built

    subgraph API["FastAPI modular monolith"]
        direction TB
        MW["Request-ID middleware + structured logging"]:::built
        AUTH["Auth: JWT login, get_current_user, require_role"]:::built
        ORD["Orders module: service + repo"]:::built
        REF["Refunds module: idempotent, approvals, payouts"]:::built
        TKT["Tickets module: service only, no HTTP route yet"]:::built
        TOOLS["Tools: registry, schema validation, executor"]:::built
        POL["Policy engine + rules.yaml: ALLOW, REQUIRE_APPROVAL, DENY"]:::built
        AGENT["Agent: orchestrator + state machine, tools view, derived keys"]:::built
        ADMIN["Admin endpoints: GET /auth/me, enriched GET /approvals, staff knowledge-base lookup"]:::built
        RAG["RAG: chunker, ingest, hybrid retriever (search_knowledge_base)"]:::built
    end

    LLM["LLM: Gemini over httpx, or the built-in fake"]:::built
    PG[("PostgreSQL 16: 16 tables, source of truth")]:::built
    VEC[("pgvector: kb_chunks vector(768), HNSW cosine + GIN full text")]:::built
    EMB["Embedder: Gemini embeddings over httpx, or the built-in fake"]:::built
    RD[("Redis: login failure counters only, disposable")]:::built
    RELAY["Outbox relay (separate process)"]:::built
    CONS["Notification consumer (separate process)"]:::built
    RESUME["Workflow-resume consumer (separate process)"]:::built
    SWEEP["Sweeper (separate process): expire approvals, purge revocations"]:::built
    RP[["Redpanda (Kafka API)"]]:::built

    UI -- "same-origin proxy" --> MW
    MW --> AGENT
    AGENT -- "messages" --> LLM
    MW --> AUTH --> ORD --> PG
    AUTH --> REF --> PG
    AUTH --> ADMIN --> PG
    ADMIN -- "staff lookup: audience from the role, internal documents included" --> VEC
    HEALTH --> PG
    HEALTH --> RD

    AGENT -- "proposes tool call, origin agent" --> TOOLS
    TOOLS --> POL
    POL -- "ALLOW" --> ORD
    POL -- "ALLOW" --> REF
    POL -- "ALLOW (read)" --> RAG
    POL -- "ALLOW" --> TKT
    POL -- "REQUIRE_APPROVAL" --> REF
    TOOLS -- "tool_calls + audit_logs" --> PG
    TKT --> PG
    AGENT -- "workflow_runs checkpoints, messages" --> PG
    RAG -- "embed the question, after commit" --> EMB
    RAG -- "audience-filtered SQL" --> VEC
    AUTH -- "failed-login counters (fails open)" --> RD
    AUTH -- "revoked_tokens: logout" --> PG
    SWEEP -- "expire, purge; refund.expired through the outbox" --> PG
    PG -- "outbox_events" --> RELAY
    RELAY --> RP --> CONS
    CONS -- "processed_events" --> PG
    RP --> RESUME
    RESUME -- "processed_events + finish the waiting turn, one transaction" --> PG
```

| Colour / line | Meaning |
|---|---|
| Green, solid | **Built** (Phases 0–6) |
| Amber | **Provisioned, not yet used.** Nothing is amber any more: Redis was the only such component and Phase 6 uses it, for the login rate limiter only (it is disposable: losing it makes the system less strict, never wrong). |
| Grey, dashed | **Planned** (Phase 7). Nothing in this diagram is planned; the answer-level evals and a re-ranker add no component to it. |

The tools have two callers: the HTTP layer (the refund and approval endpoints) and the agent. Both call the same executor with the authenticated `Actor`; the agent's calls carry `origin="agent"`, which narrows what it may run (next section). The knowledge-base search is one more tool behind that executor: it is read-only, and it is not reachable over HTTP, only through the agent and the tests. Administrators read the same documents, **internal ones included**, through a separate endpoint that is not a tool (the staff lookup): it calls the retriever directly, and the model is not involved.

## The tool pipeline

Golden rule 2 lives in one place, `app/tools/executor.py`. Every call, however it ends, leaves a `tool_calls` row and an `audit_logs` row.

```mermaid
flowchart TD
    START["T0: write tool_calls row (status pending) and COMMIT"] --> V
    V{"1. validate: schema, unknown fields forbidden"} -- "invalid" --> LOGI["log: invalid (400/422)"]
    V -- "ok" --> A
    A{"2. authorize: role allowed for this tool"} -- "no" --> LOGD["log: denied (403)"]
    A -- "yes" --> T1
    subgraph T1["T1: one database transaction"]
        direction TB
        D["3. decide: take row locks, read facts, run the policy engine"]
        D -- "DENY" --> RB["roll back T1"]
        D -- "ALLOW or REQUIRE_APPROVAL" --> E["4. execute: write the effect, audit row, outbox event"]
    end
    RB --> LOGD
    E --> COMMIT["commit T1"]
    COMMIT --> AC["5. after-commit hook, e.g. the payout (no transaction, no lock)"]
    AC --> FIN["finalize: update tool_calls row with status, result, decision, reason code"]
    LOGI --> FIN
    LOGD --> FIN
```

* **Caller identity comes only from the authenticated `Actor`** (id and role, copied from the database user row by the router). Tool inputs forbid unknown fields, so a caller cannot smuggle a `user_id` or a `role` in.
* **Registered tools:** `get_order`, `list_orders`, `create_ticket`, `cancel_order`, `request_refund`, `approve_refund`, `reject_refund`. The two admin tools are never `agent_visible`, so the future model is never even shown them.
* **The policy engine is pure.** `app/policy/engine.py` takes frozen dataclasses (rules, actor, facts, a clock value) and returns a `PolicyResult` (decision plus reason codes). No database, no clock reads, no I/O. All thresholds come from `app/policy/rules.yaml`, which is validated on load so a malformed file fails fast instead of silently weakening a rule. Precedence: `DENY` beats `REQUIRE_APPROVAL` beats `ALLOW`.
* **Stored sizes are capped.** `tool_calls.arguments` (16 KiB) and `result` (32 KiB) have `CHECK` constraints as a backstop for the executor's truncation.

## The agent

A chat message is one **turn**: one tool loop, one `workflow_runs` row. The model never executes anything: it proposes, and every proposal is handed to the executor (`origin="agent"`). State and context are checkpointed in Postgres on every transition (golden rule 4); `app/agent/fsm.py` defines the legal transitions and tests enumerate all of them.

```mermaid
flowchart TD
    START["turn starts: workflow_runs row in state PROPOSE, user message stored"] --> P
    P["PROPOSE: ask the model (no database transaction open)"] -- "text only" --> R
    P -- "proposes tool calls" --> X
    X["EXECUTE: each proposal through the executor, one at a time, derived idempotency key"] -- "results go back to the model" --> P
    X -- "tool-call cap reached" --> E
    P -- "cap already reached" --> E
    R["RESPOND: store the reply"] -- "no approval pending" --> D["DONE: status completed"]
    R -- "a refund awaits approval" --> W["WAITING_FOR_HUMAN: status waiting_approval"]
    W -- "decision is final (paid, failed, rejected, expired): refund.* event (resume consumer), or the read-time fallback; one templated message" --> D
    W -- "approved, payout still processing: stays waiting" --> W
    E["ESCALATED: the backend opens a support ticket through the executor"]
    P -- "model unreachable or error" --> F["FAILED"]
    X -- "error" --> F
```

There is no separate classification call: the model either answers in text or proposes tools. Policy and FAQ questions are answered by one more tool, `search_knowledge_base` (see [Retrieval](#retrieval-the-knowledge-base)); nothing in the machine changed when it arrived: it is an ordinary tool call in the `EXECUTE` state.

```mermaid
sequenceDiagram
    autonumber
    participant UI as Chat page
    participant R as chat router
    participant O as orchestrator
    participant DB as PostgreSQL
    participant M as LLM client
    participant X as tool executor

    UI->>R: POST /api/v1/chat with message and a client-generated message_id
    R->>O: handle_message with the authenticated Actor
    O->>DB: find a turn for this user and message_id
    alt already exists
        O-->>UI: the STORED response, Idempotent-Replayed true (no model call, no tool call)
    else new
        O->>DB: budget check, create workflow_runs row and the user message, COMMIT
        loop until the model answers in text, a cap is hit, or an error
            O->>M: system prompt, last 20 messages, agent-visible tools, max_tokens
            M-->>O: text or tool proposals
            O->>X: run each proposal, origin agent, key = agent:conversation:message:tool:args hash
            X-->>O: outcome: allowed, awaiting approval, denied, invalid or failed
            O->>DB: checkpoint state and context, store assistant and tool messages
        end
        O->>DB: store the reply, freeze the response on the workflow row, COMMIT
        O-->>UI: reply, status, tool_results and sources built by the backend, llm
    end
```

What keeps the agent inside the rules:

* **The gate.** `ToolContext.origin` is `"agent"` for model proposals. The executor refuses any tool that is not `agent_visible` before it even reads the arguments (`TOOL_NOT_AVAILABLE`, logged as a denied call), for any actor: an administrator chatting with the assistant does not give the assistant `approve_refund`. The registry also refuses to register an admin-only tool as `agent_visible`, and the model is only shown `registry.agent_tools()`.
* **What the model is shown.** `app/agent/tools_view.py` turns each tool's Pydantic schema into something a model can satisfy: money is a decimal string (the raw schema said "number or string", and a model sends `250.0`, which our validator correctly rejects), `Optional` fields are just not required, and `reason_code` is an enum taken from `rules.yaml`. The auto-approval limit is **not** told to the model. Gemini gets a second mapping to its restricted OpenAPI-style schema subset, pinned by a golden snapshot per tool.
* **Identity and keys are never model-controlled.** The actor is the authenticated user. Tool inputs forbid unknown fields, so a model-supplied `user_id`, `role` or `idempotency_key` fails validation and is logged. The refund key is derived: `agent:<conversation_id>:<message_id>:<tool>:<hash of the canonical arguments>`, so the same message always yields the same key (a retry or a repeated proposal is a replay) and a different message is judged by policy on its own.
* **Results are data.** What goes back to the model is an allow-listed, size-capped (2 KiB; 4 KiB for knowledge-base excerpts) summary built by the backend (`results_view.py`): no emails, names, notes or user ids. The UI gets a separate `tool_results` list built from the same outcomes, so what the backend decided is visible even if the model words it badly.
* **Limits.** At most 5 tool calls per turn and 20 per conversation (then `ESCALATED` and one ticket), a 90 s turn deadline, 30 turns per user per hour (`429`), `max_tokens` on every call, 20 messages of history, and no database transaction open while the model thinks.
* **Failure.** If the model is unreachable before any tool ran, the turn is undone and the caller gets `503` (the same `message_id` can be sent again). If tools already ran, their effects are real, so the turn is kept, marked `failed`, and the reply says what was done.
* **Ownership.** Conversations are the set of messages sharing a server-generated `conversation_id`; every message carries its `user_id`. Another user's conversation or workflow is `404`, administrators included.
* **Waiting for a human.** A turn that created an approval ends `waiting_approval`, with the approval and refund ids (written by the backend) in its checkpoint. It is finished by `resume.finish_waiting` (one idempotent transition, with one templated message), called from three places: the **resume consumer** on a `refund.*` event (the normal path), the orchestrator **once right after the turn enters the waiting state** (closes a race), and the **read-time fallback** `resume.sync_waiting` in `GET /api/v1/workflows/{id}` and at the start of the next message. See [Human approval](#human-approval-the-admin-page-and-event-driven-resume).

### Which model answers

`LLM_PROVIDER` selects the client (`app/agent/llm/factory.py`). `fake` (the default) is a built-in rule-based model that is deliberately **gullible**: it obeys instructions found in user text or tool results, even for tools it was not offered, so it exercises every backend defence. All tests and the demo use it (`conftest.py` forces `LLM_PROVIDER=fake` and blanks the key); outside `APP_ENV=dev` it is refused. `gemini` is `GeminiLLMClient`: one `POST` per model call to the Gemini REST API (`generateContent`, v1beta) over plain `httpx`, the key in the `x-goog-api-key` header only, a timeout and one retry for transient errors, `functionCall` / `functionResponse` parts, and `thoughtSignature` echoed back unchanged. The model name comes from `LLM_MODEL`.

## Retrieval: the knowledge base

Golden rule 3 in code: retrieved text is **data**, and `rules.yaml` always beats any document. The knowledge base lets the assistant answer "how does shipping / returns / refunds work?" with sources; it can never grant, deny or change anything.

### The pieces

| Piece | Where | What it does |
|---|---|---|
| Documents | `backend/app/data/kb/*.md` | 10 markdown files with YAML front matter (`title`, `audience`, `other_numbers`): 8 `customer`, 2 `internal`. Numbers that come from the rules are placeholders (`{{refund.auto_approve_max}}`) rendered from `rules.yaml`, and a consistency check fails on any digit or number word that is neither a placeholder nor listed in `other_numbers`, so a document cannot silently disagree with the policy engine |
| Chunker | `rag/chunker.py` | Heading-aware: splits on `#`–`###`, keeps a heading path (`Returns policy > Electronics`) with each chunk, never separates a heading from its first paragraph, keeps a list or table with the paragraph that introduces it, aims at about 300 estimated tokens, splits a long section between blocks (with a small overlap) and a long paragraph between sentences, and merges stub sections into a neighbour (token counts are estimates, words × 1.3, with no tokenizer dependency). 23 chunks for the 10 documents |
| Embedder | `rag/embedder.py`, `rag/factory.py` | An `Embedder` interface with two implementations. `GeminiEmbedder`: plain `httpx` against `batchEmbedContents` (one text per item, 768 dimensions; `gemini-embedding-001` takes a task type, `gemini-embedding-2` takes the task as a text prefix; vectors are L2-normalised; paced and retried; the key is a `SecretStr` that never reaches a URL, log or error). `FakeEmbedder`: deterministic hashed word features, no network. The fake is the default, is refused outside `APP_ENV=dev`, and is forced by `conftest.py` |
| Schema | migration `0004` | `kb_documents` (`namespace`+`source` unique, `audience` CHECK, `content_hash`, `embedding_model`) and `kb_chunks` (`embedding vector(768)` hard-coded, a generated `tsv` column, HNSW `vector_cosine_ops` index (m 16, ef_construction 64), GIN index on `tsv`, chunks deleted with their document). Hand-reviewed: exactly one named CHECK for the one enum column |
| Ingest | `rag/ingest.py`, `scripts/kb_ingest.py` | Plan (read-only) then apply. Unchanged document = no-op (hash over the rendered text, title, audience, model, dimension and chunking version). A changed document is replaced in **one transaction** after its embeddings succeeded, so a rate limit mid-run leaves the old chunks serving. Call cap, orphan pruning, dry run by default |
| Guards | `rag/guards.py` | `EMBEDDING_DIM` must equal the column width, and the namespace must hold chunks of the current model: otherwise a loud `KbDimensionMismatch` / `KbNotIndexed` / `KbConfigError` instead of an empty answer |
| Retriever | `rag/retriever.py` | Pure: a query vector and text in, chunks out. See below |
| Tool | `tools/handlers/knowledge.py` | `search_knowledge_base(query)`: read-only, `agent_visible`, roles customer and admin, policy outcome `READ_ALLOWED` |
| Results for the model | `agent/results_view.py` | Allow-listed excerpts, delimiter-wrapped, capped; plus `sources_of()` for the UI |
| Eval | `rag/eval.py`, `scripts/kb_eval.py`, `data/kb_eval/golden.yaml` | 25 golden questions and 5 out-of-scope ones |

### Ingest and a question

```mermaid
sequenceDiagram
    autonumber
    participant M as LLM (proposes)
    participant O as orchestrator
    participant X as tool executor
    participant H as knowledge handler
    participant E as embedder
    participant DB as PostgreSQL

    M-->>O: proposes search_knowledge_base(query)
    O->>X: run, origin agent, derived key
    X->>DB: log start (tool_calls), commit
    X->>X: validate (extra fields such as audience are rejected), authorize, policy READ_ALLOWED
    X->>DB: execute: record the call, COMMIT (nothing held open)
    X->>H: after_commit hook
    H->>E: embed the question (HTTP, no transaction open)
    H->>DB: guards (dimension, model), then ONE search: vector + full text, fused, thresholded, audience customer only
    H-->>X: found, results (title, heading, content, ids)
    X->>DB: finish the tool_calls row, COMMIT
    X-->>O: outcome
    O-->>M: allow-listed, delimiter-wrapped excerpts, at most 4 KiB, labelled as data
    O->>O: sources = documents of the results (computed by the backend)
```

### The search

* **Two legs.** Vector: cosine distance on the HNSW index (`SET LOCAL hnsw.iterative_scan = relaxed_order`, so a filtered scan does not run dry). Keyword: Postgres full text (`to_tsquery('english', …)` of the question's alphanumeric words, OR-ed, ranked with `ts_rank_cd`). Each leg takes a pool of 20. The query text only ever reaches the database as a bound parameter, and the tsquery is built from `[a-z0-9]` terms only.
* **Fusion.** Reciprocal Rank Fusion, `1/(60 + rank)` summed over the legs. Ties are broken by document source and chunk position, never by a random id, so a ranking is the same after a re-ingest (a test pins it).
* **"Nothing relevant" is decided on absolute scores.** RRF only ranks, so a chunk is returned only if its cosine is at least `min_cosine` **or** its keyword rank is at least `min_ts_rank`. At most 4 chunks come back. Defaults: `min_ts_rank` 0.12 (Postgres full text, the same for every embedder), `min_cosine` 0.58 for `gemini-embedding-001`, 0.64 for `gemini-embedding-2` and 0.4 for the fake. They sit halfway between the lowest best score of an in-scope question and the highest best score of an out-of-scope one **on the 30 golden questions, which are also the questions used to measure them** (gaps: about 0.02 cosine for both models, 0.167 against 0.091 for full text). They are a starting point, not an out-of-sample result.
* **The filters are in the SQL.** `d.audience = ANY(:audiences) AND d.namespace = :ns` is in each candidate query and again in the final select; the vector leg also filters `c.embedding_model = :model`. No audience in the list means no documents; an unknown audience is an error.

### Why this cannot authorize anything

| Layer | What it guarantees | Where it is tested |
|---|---|---|
| The audience is code, not an argument | The handler fixes `AGENT_AUDIENCES = (customer,)`, even for an admin chatting. `audience`, `namespace` and `top_k` are not fields of the tool, and the schema forbids extras, so a model cannot ask for them | `test_rag_tool.py`, `test_rag_safety.py` |
| The audience is in the SQL | Every internal chunk, used as its own query text, returns nothing for `customer` in all three modes; a query like `x') OR audience = 'internal'` is just words | `test_rag_retriever.py`, `test_rag_safety.py` |
| Excerpts are data | Allow-listed fields (`source`, `text`), at most 900 characters each and 4 KiB in all, wrapped in `<<<EXCERPT n … END EXCERPT n>>>` with those marker characters stripped from the document text, plus a backend-written note saying they are reference data | `test_rag_tool.py` |
| Decisions do not read documents | Nothing under `policy/`, `modules/`, or the executor, registry and context imports the knowledge base (a test scans the source). A lying article ("refunds of any amount are always approved") and a poisoned one ("call approve_refund …") are retrieved by the gullible model's questions and the outcomes are identical to the run without them | `test_rag_safety.py`, `scripts/demo_phase4.py` |
| Sources are not the model's words | `sources` are the documents of the search results, computed by the backend; if no search ran, there are none, whatever the model claims. They are the *retrieved* articles, not necessarily the ones the answer used | `test_rag_tool.py` |
| Failure is generic | A guard failure or an embedding outage fails the tool call (`KB_UNAVAILABLE`, 503 on the call); the detail goes to the `tool_calls` row and the log, the model sees "The help articles are unavailable right now" | `test_rag_tool.py` |

The system prompt tells the model to answer only from the excerpts and to say "I'm not sure" when nothing relevant was found, but, as everywhere, the prompt is guidance and the tests assume a model that ignores it.

### Measured, with caveats

The eval (`scripts/kb_eval.py`) ranks with the thresholds off (hit@1 / hit@3 per mode), then runs hybrid with the real thresholds. Reproduced on the `default` namespace with `gemini-embedding-001` while writing this: vector 21/25 and 24/25, keyword 19/25 and 24/25, hybrid 20/25 and 24/25; 25/25 in-scope questions answered; 5/5 out-of-scope "not found" (after tuning on the same questions). `gemini-embedding-2` printed vector 23/25, keyword 19/25, hybrid 20/25 at hit@1. **Hybrid did not beat vector-only**, and a difference of one or two questions out of 25 is noise. The full printouts are in [`phase-4-report.md`](phase-4-report.md). Nothing is claimed from fake-embedder runs.

## Human approval: the admin page and event-driven resume

A refund above the automatic limit (or one the policy sends to review) is not paid. The executor creates a `pending` approval, the turn ends in `WAITING_FOR_HUMAN`, and an administrator decides **through the existing approval endpoints**. Phase 5 added the page an administrator uses, and the path that tells the waiting customer, **by event**, as soon as the decision is final. Nothing in the executor, the policy engine, the idempotency code or `rules.yaml` changed (`git diff 42ffd59..HEAD` shows no change to any of them).

```mermaid
sequenceDiagram
    autonumber
    participant C as Chat page (customer)
    participant A as Admin page /admin
    participant API as FastAPI
    participant DB as PostgreSQL
    participant RL as outbox relay
    participant RP as Redpanda
    participant RC as workflow-resume consumer

    C->>API: POST /chat, a 700 refund
    API->>DB: refund pending_approval, approval row, turn WAITING_FOR_HUMAN with approval_id and refund_id in the checkpoint
    API->>DB: finish_waiting once, right after the commit (the race closure): nothing decided yet, nothing changes
    API-->>C: status waiting_approval
    A->>API: GET /auth/me, then GET /approvals (admin only)
    API-->>A: role from the database, items with refund, order, review_reasons and requester_refunds_in_window
    A->>API: POST /approvals/id/approve (the existing endpoint: executor, policy, SELF_APPROVAL check)
    API->>DB: decision, then the payout after the commit, refund.approved and refund.completed in the outbox, each in the transaction of its change
    RL->>RP: publish (key = refund id)
    RP->>RC: refund.approved, refund.completed (at least once)
    RC->>DB: ONE transaction: claim in processed_events, find the turn waiting for this refund and this customer, and finish it only if waiting_for is none
    RC->>RP: commit the offset after the transaction
    C->>API: GET /workflows/id, every 15 s
    API-->>C: completed, latest_message "Update: ...", waiting_for null
```

**What the page is.** `frontend/app/admin/page.tsx` lists approvals (pending, approved, rejected, expired) from `GET /api/v1/approvals`, shows who is signed in from `GET /api/v1/auth/me`, and sends Approve and Reject to the **same** `/approvals/{id}/approve|reject` endpoints any API client would use. Every refusal (`SELF_APPROVAL`, already decided, wrong role) is the backend's own answer shown as it is. A customer who opens `/admin` is shown "You do not have access"; that is display only, the API answers `403` on its own. `GET /auth/me` reads the role from the database, like every other request. The enriched list adds the refund and order summary, `review_reasons` (as the policy engine reported them when the refund was requested, stored in the approval's payload) and `requester_refunds_in_window` (the same count the policy's velocity rule uses), with ids only and no emails. `approvals.expires_at` is the deadline: `created_at` plus `APPROVAL_TTL_HOURS` for a pending approval (computed by the backend, shown as "decide by"), the deadline it passed once the sweeper expired it. Expiry itself is described in [Hardening](#hardening-login-limiter-revocation-expiry-observability); the Expired tab lists those approvals (in Phase 5 it was always empty).

**The staff knowledge-base lookup.** `GET /api/v1/admin/knowledge/search` is `require_role(admin)`, so a customer is refused before anything is embedded. The audience comes from the authenticated role in code (`rag/audience.py`: `audiences_for_role`: an administrator gets `customer` and `internal`, anyone else `customer`), never from the request, and the retriever enforces it inside the SQL as before. It writes an `admin.kb_search` audit row (who, the question's length, how many results, how many internal) without the question's text, and holds no transaction open while the question is embedded. The assistant's own `search_knowledge_base` is untouched and stays customer-only, even when an administrator is chatting.

**The resume consumer** (`python -m app.events.consumers.resume`, consumer name `workflow-resume`) is a separate process on the same `EventConsumer` base as the notification consumer, reading the refund topic (`ops.refunds` by default). It handles `refund.approved`, `refund.rejected`, `refund.completed` and `refund.failed` and ignores every other event type. From the event it takes ids only (`refund_id`, `customer_id`); amounts, the decision and the payout status come from the database. It finds the workflow with `status = 'waiting_approval' AND context ->> 'refund_id' = :id` (the partial index of migration `0005`) and the customer's `user_id`, and calls `finish_in_transaction` inside the dedupe transaction: the `processed_events` claim and the finish commit together, so a duplicate delivery does nothing; the Kafka offset is committed afterwards; a message that can never be processed goes to the dead-letter topic. It **never calls the model, never runs a tool and moves no money**; it writes the workflow's state and one assistant message, and nothing else. With no waiting workflow it is a no-op.

**`waiting_for` and the payout rule.** `resume.waiting_for(approval, refund)` says what a waiting turn still waits for: `"approval"` (no decision yet, or a checkpoint the backend cannot verify), `"payout"` (approved, but the refund is still `processing`), or `None` (final). Only a final turn is finished. An approved refund whose payout is still `processing` **stays waiting**; the later `refund.completed` or `refund.failed` event (the reconcile path emits them too) finishes it, so the customer is told "paid" rather than "being processed". The backend returns `waiting_for` in `GET /workflows/{id}` and the page only shows it ("Waiting for human approval" or "Approved, payout in progress").

**The race closure.** The model's final answer takes seconds, and the turn only becomes `waiting_approval` when it commits. An administrator who decides in that window produces an event that finds no waiting workflow, and the consumer would do nothing. So the orchestrator calls `finish_waiting` once right after it commits a waiting turn: a decision committed before that point is seen there, and one committed after it reaches a workflow that is now waiting. Both can happen; the row lock and the status re-check make the second a no-op. Tests force the window with a model whose second call lets an admin decide.

**The read-time fallback is kept.** `GET /workflows/{id}` and the start of the customer's next message still call `sync_waiting`, which is the same `finish_waiting` step. If the relay or the consumer is stopped the customer still gets the answer on the next poll (the page polls immediately, then every 15 s). **That can hide a dead consumer**: the customer sees the right result either way. This is why the Phase 5 demo reads the **database** before it makes any `GET`, and then stops the consumer on purpose to show the fallback doing the work. Phase 6 adds `GET /api/v1/admin/pipeline`, which shows a dead consumer (a turn still waiting long after a final decision) to an administrator who looks; nothing alerts by itself.

## Hardening: login limiter, revocation, expiry, observability

Phase 6 adds four things around the edges of the system. None of them touches the executor, the policy engine, the idempotency code or `rules.yaml`: the limiter and the revocation check live in the auth dependency and router, expiry in a new module and a new process, and the observability work in the middleware and one read-only admin endpoint.

### Login rate limiting (Redis, fails open)

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as POST /auth/login
    participant L as LoginLimiter
    participant R as Redis
    participant DB as PostgreSQL

    C->>A: email and password (maybe X-Forwarded-For)
    A->>A: client_ip: the socket peer, or the entry TRUSTED_PROXY_HOPS from the right
    A->>L: retry_after(email, ip)
    L->>R: GET key = hash of email and ip (0.5 s timeout)
    alt Redis down or slow
        L-->>A: None, a warning is logged (FAILS OPEN)
    else counter has reached the maximum
        L-->>A: seconds left in the window
        A-->>C: 429 with Retry-After, before any password work
    else below the maximum
        L-->>A: None
    end
    A->>DB: authenticate: argon2 verification (a dummy hash for an unknown email)
    alt wrong password, unknown email or inactive account
        A->>L: record_failure (one Lua script: INCR, and a TTL if there is none)
        A-->>C: 401 Invalid email or password
    else valid
        A->>L: reset
        A-->>C: 200 and a token with its own jti
    end
```

The key holds a hash of the normalised email and the IP, never the email. A blocked attempt does no password work and does not count, so it cannot extend the window. **The limit is per email and IP: an attacker rotating through many IPs can try the maximum number of passwords from each one against a single email; that is a known limit, not covered here.** The IP is the socket peer unless `TRUSTED_PROXY_HOPS` says that many proxies you control append to `X-Forwarded-For`; the entry that many hops from the right is used. The Next.js dev proxy forwards the client's own header and appends nothing (checked), so the setting must stay 0 behind it: every browser then looks like `127.0.0.1`, and the limit is effectively per email.

### Logout and token revocation (Postgres)

Every token carries a unique `jti`. `POST /auth/logout` inserts it into `revoked_tokens` (with the token's own expiry) and commits. `get_current_token`, which `get_current_user` depends on, validates the signature, algorithm and expiry first and then looks the `jti` up in that table, so **every authenticated route refuses a revoked token**; a test walks every route and fails if one authenticates another way. It is a table and not a Redis denylist because revocation is security state: Redis is disposable (golden rule 4), losing it must not un-revoke anything, and the user is already read from Postgres on every request, so one primary-key lookup adds no new failure mode. The sweeper deletes rows whose token has expired on its own. A token without a `jti` is refused (it could never be revoked), so sessions that were open when Phase 6 was deployed end once.

### Approval expiry (the sweeper)

```mermaid
sequenceDiagram
    autonumber
    participant SW as sweeper (separate process)
    participant DB as PostgreSQL
    participant RL as outbox relay
    participant RP as Redpanda
    participant RC as workflow-resume consumer
    participant AD as admin (approve or reject)

    Note over DB: an approval was created long ago and is still pending
    AD->>DB: approve or reject, after the deadline but before the next sweep
    DB-->>AD: 409 APPROVAL_EXPIRED (the handler compares the deadline with its own clock)
    SW->>DB: due approvals, oldest first, no locks
    loop one approval per transaction
        SW->>DB: lock order, user, refund, approval, and re-check pending and past the deadline
        SW->>DB: approval expired (nobody decided), refund expired, refund.expired in the outbox, an audit row, COMMIT
    end
    RL->>RP: publish refund.expired (key = refund id)
    RP->>RC: refund.expired (at least once)
    RC->>DB: ONE transaction: claim in processed_events and finish the waiting turn, "expired without a decision"
    AD->>DB: approve or reject again
    DB-->>AD: 409 APPROVAL_EXPIRED (the status is expired)
```

The deadline is `created_at + APPROVAL_TTL_HOURS` (a setting, not a rule in `rules.yaml`). **There is no window in which a late decision slips through**: once the sweeper has run, the status says `expired`; before it runs, `_lock_for_decision` refuses a pending approval whose deadline has passed (approve and reject alike). The refund gets a new status, `expired` (migration `0007`); it never moved money, and `rules.yaml` lists by inclusion the statuses that hold money and count toward the velocity limit, so expiry releases both without a policy change. The customer is told by the resume consumer, or by the read-time fallback if the consumer is down, with the same words. **If the sweeper is not running, approvals never expire.** It is idempotent and safe to run twice (the re-check under the locks), and `python -m app.jobs.sweeper --once` does one full sweep with a hard deadline.

### Observability

* **Request ids.** `RequestIDMiddleware` honours a valid inbound `X-Request-ID` (`[A-Za-z0-9_-]{8,64}`), otherwise generates one, binds it to every log line of the request, echoes it in the response header and writes one access line. New: an unhandled error is caught there, logged with its traceback while the id is still bound, and answered with a generic `500` that carries the id in the header and the body (never the exception's text); Starlette would have built that response outside the middleware, so it had no id. The id also reaches `tool_calls`, `audit_logs` and the outbox event's `meta`.
* **`GET /health`** pings Postgres and Redis with a 2 s timeout each, answers `200` or `503` and names the dependency that is down, without connection details. It is unauthenticated and not rate limited.
* **`GET /api/v1/admin/pipeline`** (admin only, read-only, **deliberately not part of `/health`**: a lagging relay must never make a liveness probe restart the API) reads the database for the symptoms of a stopped background process: unpublished outbox events that are old, parked events (the outbox through the ORM model, because only the relay may touch that table in SQL), chat turns still waiting although their approval is already final (a dead resume consumer, which the read-time fallback otherwise hides; approved-and-still-processing is not final), and pending approvals past their deadline by more than the sweeper's interval allows (a stopped sweeper). It returns counts and ages only, `status` `ok` or `attention`, and the reasons.

### Browser-facing hardening

| Where | What |
|---|---|
| `frontend/next.config.ts`, every page, the 404 and the static assets | `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Permissions-Policy` (camera, microphone, geolocation, payment, usb off), `Cross-Origin-Opener-Policy: same-origin`; no `X-Powered-By` |
| The CSP | `default-src 'self'`; `script-src 'self' 'unsafe-inline'`; `style-src 'self' 'unsafe-inline'`; `img-src 'self' data:`; `font-src 'self'`; `connect-src 'self'`; `object-src 'none'`; `base-uri 'self'`; `form-action 'self'`; `frame-ancestors 'none'`. **Development only** adds `'unsafe-eval'` and `ws:` (React Refresh and the hot-reload websocket) |
| `app/api/middleware.py` (`SecurityHeadersMiddleware`), every API response | `X-Content-Type-Options: nosniff`; and `Cache-Control: no-store` under `/api/` unless the endpoint set its own |
| CORS | None. There is no CORS middleware and no `Access-Control-*` header: the browser talks only to the Next.js origin, which proxies `/api/*` (the destination is fixed by `API_ORIGIN`, the request path cannot choose another host), and a cross-origin page cannot read an API response |
| `app/main.py` | With `APP_ENV=prod` there is no `/docs`, `/redoc` or `/openapi.json` |
| Not set, on purpose | HSTS (plain HTTP; it belongs where TLS ends), a nonce-based CSP (it would need middleware and dynamic rendering of every page; `'unsafe-inline'` for scripts is the documented trade-off), a trusted-host list (nothing builds URLs from the `Host` header) |

Next's headers do not apply to the proxied `/api/*` responses; those carry the API's own (`nosniff`, `no-store`). Decision 50 has the reasoning, and the real-browser check that nothing breaks.

## Request flows as implemented today

### Login: `POST /api/v1/auth/login`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant MW as RequestIDMiddleware
    participant R as auth router
    participant S as users service
    participant DB as PostgreSQL

    C->>MW: POST /api/v1/auth/login (email, password)
    MW->>MW: keep X-Request-ID only if it matches the allowed pattern, else generate one
    MW->>R: request (request_id bound to every log line)
    R->>S: authenticate(email, password)
    S->>DB: SELECT user WHERE email = normalised email
    S->>S: argon2 verify against the user's hash, or against a dummy hash if no such user
    alt unknown email, wrong password or inactive account
        S-->>R: None
        R-->>C: 401 Invalid email or password (identical body in all three cases)
    else valid credentials
        opt stored hash uses outdated argon2 parameters
            S->>DB: UPDATE password_hash and COMMIT
        end
        S-->>R: user
        R->>R: create HS256 token with sub, iat and exp (now + 30 min)
        R-->>C: 200 access_token, token_type bearer, expires_in 1800
    end
    Note over MW,C: Every response carries X-Request-ID and one access-log line is written
```

### Reading one order: `GET /api/v1/orders/{order_id}`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant MW as RequestIDMiddleware
    participant D as get_current_user
    participant R as orders router
    participant OS as orders service
    participant OR as orders repo
    participant DB as PostgreSQL

    C->>MW: GET /api/v1/orders/{order_id} with Bearer token
    MW->>D: request (request_id bound)
    D->>D: decode JWT (HS256 only, exp and sub required)
    alt token missing, malformed, expired or badly signed
        D-->>C: 401 Not authenticated
    end
    D->>DB: SELECT user WHERE id = sub
    alt user missing or inactive
        D-->>C: 401 Not authenticated
    end
    Note over D,DB: The role used from here on is the one in this database row, never a claim in the token
    D->>R: current user
    R->>R: validate order_id as a UUID (422 if malformed)
    R->>OS: get_order(user, order_id)
    OS->>OS: owner filter = none for admin, otherwise the caller's own user id
    OS->>OR: get_order(order_id, owner filter)
    OR->>DB: SELECT order and items WHERE id = order_id AND user_id = owner filter
    alt no row (order does not exist OR belongs to someone else)
        R-->>C: 404 Order not found (the same response for both)
    else row found
        R-->>C: 200 order JSON (money as exact decimal strings)
    end
```

Authentication always runs before path validation, so an unauthenticated caller gets `401` even for a malformed id. The list endpoint (`GET /api/v1/orders`) follows the same path with the owner filter applied to both the page query and the total count.

### Requesting a refund: `POST /api/v1/refunds`

This is the Phase 2 path end to end for a refund within policy. The full response table is in [`refunds-api.md`](refunds-api.md).

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant R as refunds router
    participant X as tool executor
    participant H as request_refund handler
    participant P as policy engine
    participant DB as PostgreSQL
    participant PV as payment provider

    C->>R: POST /api/v1/refunds with Idempotency-Key and Bearer token
    R->>R: authenticate, copy the user into a plain Actor, validate the header and body
    R->>X: execute request_refund with the Actor
    X->>DB: T0: insert tool_calls row, COMMIT
    X->>X: validate, then authorize the role
    X->>DB: T1: BEGIN
    X->>H: decide
    H->>DB: lock the order, then the customer's user row
    H->>DB: look up an existing refund for this user and key
    alt key already used
        H-->>X: same payload: replay the stored refund (200). Different payload: 409
    else new request
        H->>DB: read payment, amount already refunded, refunds in the last 30 days
        H->>P: evaluate_refund_request(rules, actor, facts, now)
        P-->>H: ALLOW, REQUIRE_APPROVAL or DENY, with reason codes
        alt DENY
            X->>DB: roll back T1, record the denial in tool_calls and audit_logs (nothing else is created)
            X-->>C: 403 code, decision DENY, reason
        else ALLOW or REQUIRE_APPROVAL
            H->>DB: INSERT refund ON CONFLICT DO NOTHING (UNIQUE requested_by, idempotency_key)
            H->>DB: outbox event refund.requested, audit row
            opt REQUIRE_APPROVAL
                H->>DB: INSERT approval (status pending)
            end
            opt ALLOW
                H->>DB: outbox event refund.approved
            end
            X->>DB: COMMIT T1
        end
    end
    opt ALLOW: payout runs after the commit
        X->>DB: phase 1: read what the provider needs, commit (no lock kept)
        X->>PV: refund with key refund-uuid (no transaction, no lock held)
        PV-->>X: succeeded, declined, or no answer
        X->>DB: phase 3: lock the refund, set status, outbox event refund.completed or refund.failed
    end
    X->>DB: finalize the tool_calls row
    X-->>C: 201 succeeded or failed, 202 pending_approval or processing
```

`POST /api/v1/approvals/{id}/approve` is the same machinery: the executor runs `approve_refund`, whose handler takes the locks in the documented order, re-reads the approval and refund under them, asks the policy engine (an admin may not approve their own request: `SELF_APPROVAL` is a `DENY`), emits `refund.approved`, and the same payout hook runs after the commit.

### From database row to notification: the event pipeline

```mermaid
sequenceDiagram
    autonumber
    participant DB as PostgreSQL
    participant RL as outbox relay
    participant RP as Redpanda
    participant CO as notification consumer
    participant DL as dead-letter topic

    Note over DB: A request committed a state change and its outbox_events row in ONE transaction
    loop every poll interval
        RL->>DB: SELECT due, unpublished, unparked rows FOR UPDATE SKIP LOCKED, skipping any row with an earlier unpublished row of the same aggregate
        RL->>RP: produce (key = aggregate id, acks all, idempotent producer)
        alt acknowledged
            RL->>DB: mark published
        else failed
            RL->>DB: attempts + 1, next attempt with exponential backoff, parked after max attempts
        end
    end
    RP->>CO: message (at least once)
    CO->>CO: validate envelope version and the ids-only field list
    alt malformed or unknown version
        CO->>DL: copy to the dead-letter topic with the reason
        CO->>RP: commit the offset (skip it)
    else valid
        CO->>DB: one transaction: insert processed_events(consumer, event_id), run the effect
        alt event_id already in processed_events
            CO->>CO: duplicate delivery, skip
        end
        CO->>RP: commit the offset AFTER the transaction
    end
```

The diagram shows the notification consumer. The **workflow-resume consumer** is a second consumer of the same topic on the same base class, with the same steps and guarantees (claim and effect in one transaction, offset after, dead letters); its effect is described in [Human approval](#human-approval-the-admin-page-and-event-driven-resume). Details, failure handling and the event schema are in [`events.md`](events.md) (written in Phase 2: it does not cover the resume consumer). `relay --once`, `consumers.notifications --once` and `consumers.resume --once` have a hard overall deadline (default 120 s; exit code 3 with a message naming what they were waiting on) so a hang is a visible failure.

## Concurrency and locking

* **One lock order, defined in one file** (`app/db/locking.py`): **order → user → refund → approval**. Anything that takes more than one of these takes them in that order, so transactions can wait on each other but never in a cycle. The locks are `FOR NO KEY UPDATE`, which conflict with each other but not with the `FOR KEY SHARE` locks foreign-key checks take.
* **The order lock** serialises refunds and cancellations on one order, so "remaining refundable" cannot be over-committed. **The user lock** serialises the per-customer velocity count across their different orders.
* **Idempotency is decided by the database.** `UNIQUE(requested_by, idempotency_key)` plus `INSERT … ON CONFLICT DO NOTHING`: ten identical concurrent requests produce exactly one refund row and one provider ledger entry (a test fires ten at once). A request hash separates "same key, same payload" (replay) from "same key, different payload" (409).
* **No external call inside a transaction or under a lock.** The provider call is made between two short transactions, and its idempotency key is `refund-<refund uuid>`, never the caller's key. An answer that never arrives leaves the refund `processing`; `scripts/reconcile_refunds.py` re-checks those.
* **Chat turns are serialised by the database too.** `UNIQUE (user_id, message_id)` and `UNIQUE (conversation_id, message_id)` on `workflow_runs` decide "the same message twice" (six simultaneous identical requests make one workflow and one refund; the others replay or get `409`), and a partial unique index allows at most one `running` turn per conversation. A turn that died with its process is marked `failed` once it is older than the turn deadline plus slack. Ten different messages asking for 400.00 of a 1000.00 order at once end with exactly two refunds: the order lock still decides.
* **Expiry takes the same locks as a decision.** The sweeper's `expire_one` locks order -> user -> refund -> approval and re-checks the approval and refund status and the deadline under them, so a decision and the sweeper racing, or two sweepers, can never both win (tested, including many contenders on one order: no deadlock). The candidate query takes no locks.
* **The relay is safe to run twice** (`SKIP LOCKED`), and the per-aggregate ordering check means a refund's events are published in order, even when a retry or a second relay is involved.

## Data model

Sixteen tables, created by the Alembic migrations `0001` (the eleven Phase 1 tables), `0002` (Phase 2: per-user refund idempotency and `request_hash`, new refund and tool-call states, outbox ordering and parking, the `processed_events` table), `0003` (Phase 3: the `messages` table, and conversation / message identity on `workflow_runs` plus the `escalated` status), `0004` (Phase 4: `kb_documents` and `kb_chunks`), `0005` (Phase 5: one partial expression index on `workflow_runs`, no table, column or enum), `0006` (Phase 6: `revoked_tokens`) and `0007` (Phase 6: the refund status `expired`, a replaced `CHECK` on a `varchar` column, so no column change). The diagram below was regenerated from the SQLAlchemy metadata (`Base.metadata`) when this was written; **Phase 6 added exactly one table** to it (`revoked_tokens`), and the `expired` status is a value, not a column. It matches the code. Types are simplified: `varchar` columns holding a fixed set of values (`status`, `role`, `decision`, …) are enforced by a named `CHECK` constraint; every money column is `numeric(12,2)` next to a `currency` column; all primary keys are UUIDs generated by the application.

```mermaid
erDiagram
    approvals {
        uuid workflow_run_id FK
        uuid tool_call_id FK
        uuid refund_id FK
        uuid requested_by FK
        uuid decided_by FK
        varchar action
        jsonb payload
        varchar status
        text decision_reason
        timestamptz decided_at
        timestamptz expires_at
        uuid id PK
        timestamptz created_at
        timestamptz updated_at
    }
    audit_logs {
        uuid actor_user_id FK
        varchar action
        varchar resource_type
        varchar resource_id
        varchar decision
        varchar reason_code
        text reason
        jsonb details
        varchar request_id
        timestamptz created_at
        uuid id PK
    }
    kb_chunks {
        uuid document_id FK
        int ordinal
        text heading
        text content
        vector embedding
        tsvector tsv
        varchar embedding_model
        int token_count
        uuid id PK
    }
    kb_documents {
        varchar namespace
        varchar source
        varchar title
        varchar audience
        int version
        varchar content_hash
        varchar embedding_model
        timestamptz created_at
        timestamptz updated_at
        uuid id PK
    }
    messages {
        int seq
        timestamptz created_at
        uuid conversation_id
        uuid user_id FK
        uuid workflow_run_id FK
        varchar role
        jsonb content
        uuid id PK
    }
    order_items {
        uuid order_id FK
        varchar sku
        varchar name
        int quantity
        numeric unit_price
        timestamptz created_at
        uuid id PK
    }
    orders {
        uuid user_id FK
        varchar status
        numeric total_amount
        varchar currency
        timestamptz placed_at
        uuid id PK
        timestamptz created_at
        timestamptz updated_at
    }
    outbox_events {
        int seq
        varchar aggregate_type
        varchar aggregate_id
        varchar event_type
        jsonb payload
        timestamptz created_at
        timestamptz published_at
        int attempts
        timestamptz next_attempt_at
        timestamptz parked_at
        text last_error
        uuid id PK
    }
    payments {
        uuid order_id FK
        numeric amount
        varchar currency
        varchar status
        varchar provider
        varchar provider_ref
        uuid id PK
        timestamptz created_at
        timestamptz updated_at
    }
    processed_events {
        varchar consumer PK
        uuid event_id PK
        timestamptz processed_at
    }
    refunds {
        varchar idempotency_key
        varchar request_hash
        uuid payment_id FK
        uuid order_id FK
        uuid requested_by FK
        numeric amount
        varchar currency
        varchar reason_code
        varchar status
        text reason
        timestamptz processed_at
        uuid id PK
        timestamptz created_at
        timestamptz updated_at
    }
    revoked_tokens {
        varchar jti PK
        uuid user_id FK
        timestamptz expires_at
        timestamptz revoked_at
    }
    tickets {
        uuid user_id FK
        uuid order_id FK
        varchar subject
        text body
        varchar status
        varchar priority
        uuid id PK
        timestamptz created_at
        timestamptz updated_at
    }
    tool_calls {
        uuid workflow_run_id FK
        uuid user_id FK
        varchar tool_name
        jsonb arguments
        varchar status
        varchar policy_decision
        varchar policy_reason_code
        text policy_reason
        jsonb result
        varchar error_code
        text error
        varchar request_id
        timestamptz created_at
        timestamptz finished_at
        uuid id PK
    }
    users {
        varchar email UK
        varchar password_hash
        varchar full_name
        varchar role
        bool is_active
        uuid id PK
        timestamptz created_at
        timestamptz updated_at
    }
    workflow_runs {
        uuid user_id FK
        varchar status
        varchar state
        jsonb context
        text error
        uuid conversation_id
        uuid message_id
        jsonb response
        timestamptz completed_at
        uuid id PK
        timestamptz created_at
        timestamptz updated_at
    }

    kb_documents ||--o{ kb_chunks : "document_id"
    orders |o--o{ tickets : "order_id"
    orders ||--o{ order_items : "order_id"
    orders ||--o{ payments : "order_id"
    orders ||--o{ refunds : "order_id"
    payments ||--o{ refunds : "payment_id"
    refunds |o--o{ approvals : "refund_id"
    tool_calls |o--o{ approvals : "tool_call_id"
    users |o--o{ approvals : "decided_by"
    users |o--o{ audit_logs : "actor_user_id"
    users ||--o{ approvals : "requested_by"
    users ||--o{ messages : "user_id"
    users ||--o{ orders : "user_id"
    users ||--o{ refunds : "requested_by"
    users ||--o{ revoked_tokens : "user_id"
    users ||--o{ tickets : "user_id"
    users ||--o{ tool_calls : "user_id"
    users ||--o{ workflow_runs : "user_id"
    workflow_runs |o--o{ approvals : "workflow_run_id"
    workflow_runs |o--o{ messages : "workflow_run_id"
    workflow_runs |o--o{ tool_calls : "workflow_run_id"
```

Which tables application code uses today:

| Table | Today |
|---|---|
| `users` | Read by login and `get_current_user`; written by login (hash upgrade) and the seed |
| `orders`, `order_items` | Read by the orders API and the policy facts; `cancel_order` updates orders; written by the seed |
| `payments` | Read by refunds and the reconcile job; written by the seed |
| `refunds`, `approvals` | Written and read by the refund and approval tools; read (with the order) by the enriched admin approvals list and by the resume code to decide whether a waiting turn is final |
| `tool_calls`, `audit_logs` | Written by the executor for **every** call, including denied and invalid ones |
| `outbox_events` | Written in the same transaction as each refund or ticket change; read and updated by the relay and `outbox_admin.py` |
| `revoked_tokens` | Written by `POST /auth/logout`, read by `get_current_token` on every authenticated request, purged by the sweeper once the token has expired on its own |
| `processed_events` | Written by the consumers, the notification consumer and the resume consumer (the de-duplication ledger) |
| `tickets` | Written by the `create_ticket` tool (no HTTP route yet) |
| `workflow_runs` | One row per chat turn: state and context (the checkpoint), conversation and message ids, the frozen response; written and read by the orchestrator and `GET /workflows/{id}`; a waiting turn is found by refund id and finished by the resume consumer (or the fallback) |
| `messages` | The chat transcript (user, assistant and tool messages as JSON blocks); written by the orchestrator, read to build the model's history |
| `kb_documents`, `kb_chunks` | The knowledge base: written only by `scripts/kb_ingest.py`, read by the `search_knowledge_base` tool's SQL. Not touched by any request handler or by policy code |

Constraints that carry weight:

* `refunds`: `UNIQUE(requested_by, idempotency_key)`, `amount > 0`, `request_hash` (SHA-256 of the canonical payload).
* `outbox_events`: a unique identity column `seq` (strict publish order), `next_attempt_at`, `attempts`, `parked_at`, and partial indexes for "due and unpublished" and "earlier unpublished for this aggregate".
* `approvals`: at most one *pending* approval per refund (partial unique index). `status` includes `expired` (set by the sweeper: `decided_by` stays empty, `expires_at` records the deadline).
* `refunds.status`: one named CHECK (`ck_refunds_refund_status`) whose values are `pending_approval`, `processing`, `succeeded`, `failed`, `rejected` and, since `0007`, `expired`. `rules.yaml` lists the statuses that hold money and count toward the velocity limit by inclusion, and `expired` is not in them, so an expired refund releases both.
* `revoked_tokens`: primary key `jti`, `user_id` foreign key `ON DELETE CASCADE`, an index on `expires_at` for the purge, no enum and so no CHECK.
* `tool_calls`: size caps on `arguments` and `result`.
* `processed_events`: primary key `(consumer, event_id)`, which makes a duplicate delivery a no-op.
* `workflow_runs`: `UNIQUE (conversation_id, message_id)`, `UNIQUE (user_id, message_id)`, and a partial unique index allowing one `running` turn per conversation; the `workflow_status` CHECK includes `escalated`. Migration `0005` adds `ix_workflow_runs_waiting_refund`, a partial expression index on `(context ->> 'refund_id') WHERE status = 'waiting_approval'`, so the resume consumer's lookup touches only the few turns that are waiting however many turns have run (the downgrade drops it).
* `messages`: role CHECK (`user`, `assistant`, `tool`), a 32 KiB cap on `content`, and an identity column `seq` for strict order; deleting a user that has messages is refused (`RESTRICT`).
* `kb_documents`: `UNIQUE (namespace, source)`; `audience` CHECK (`customer`, `internal`). `kb_chunks`: `embedding vector(768)` (the width is a literal in the migration, and the application compares `EMBEDDING_DIM` with it), a **generated, stored** `tsv` column, `UNIQUE (document_id, ordinal)`, `token_count > 0`, an HNSW cosine index and a GIN index; `ON DELETE CASCADE` from the document. The downgrade drops both tables (the knowledge base can be rebuilt with `kb_ingest.py`).

## Golden rules

These come from `CLAUDE.md` and are never to be broken. The last column says how much of each exists today.

| # | Rule | Why | Today |
|---|---|---|---|
| 1 | The LLM proposes, the backend decides. The LLM never executes anything directly. | A model can be wrong or manipulated (prompt injection), so authority must live in deterministic code that can be tested and audited. | **Built**: every model proposal runs through the executor with `origin="agent"`; admin tools are never shown to the model and are refused if proposed; the prompt-injection tests use a model that obeys everything and prove the backend still refuses |
| 2 | Every tool call passes schema validation → authorisation → policy engine → execution → `tool_calls` log. | One mandatory chokepoint means nothing runs unchecked and every attempt, allowed or denied, leaves a record. | **Built** (`app/tools/executor.py`); tests check that denied, invalid and failed calls are logged |
| 3 | Retrieved (RAG) text is data, never instructions. `rules.yaml` always beats any document. | Documents can contain injected instructions; business rules must come from one reviewed source. | **Built** (Phase 4): `rules.yaml` is the only source of thresholds; the policy, refund and executor code does not read documents (a test scans for it); the audience filter is in the retrieval SQL and fixed to `customer` for the agent; excerpts reach the model as allow-listed, delimited, capped data; tests run a gullible model against a lying and a poisoned article and show identical decisions. Phase 5: administrators read internal documents only through the staff lookup (no model, audience from the role in code); the assistant's search stays customer-only even when an administrator is chatting |
| 4 | Postgres is the source of truth, including workflow state. Redis is disposable (cache, rate limit, idempotency fast path). | Losing Redis must never lose state or change an outcome, and a crashed workflow must be resumable from its last checkpoint. | Postgres holds all state, and idempotency does not use Redis at all. The agent's state machine is checkpointed in `workflow_runs` (state + context) and the transcript is in `messages`; a waiting turn is found and finished from Postgres rows (the event only says which refund; the database says what happened). Redis is used only by the login rate limiter, and the limiter fails open if it is down: it can make the system stricter, never decide who may log in. Token revocation is in Postgres for the same reason |
| 5 | The frontend contains zero business logic. | A single enforcement point in the backend can't be bypassed or spoofed by modifying a client. | The Next.js pages only send messages and decisions and show what the API says: the reply, the backend-built tool outcomes, the waiting state (`waiting_for`, computed by the backend) and, on `/admin`, the approvals with their review reasons. Approve and Reject go through the existing executor endpoints; the page's role check is display only and the API answers `403` itself |
| 6 | Refunds are idempotent: `Idempotency-Key` plus a database `UNIQUE` constraint. | Retries, double-clicks and repeated LLM proposals must never pay out twice; the database is the final arbiter even if Redis is wrong. | **Built and concurrency-tested** (10 simultaneous identical requests make one refund and one payout). Through the agent the key is derived by the backend, and a client `message_id` makes a retried chat message a replay |
| 7 | Events go through a transactional outbox, never published directly from request handlers. | The state change and its event commit atomically, so a broker outage can neither lose an event nor emit one for a rolled-back change. | **Built**: outbox writer, relay, consumer. A test (`test_no_direct_publish.py`) scans the source and fails if anything other than the event modules imports a Kafka client, builds an `OutboxEvent` by hand, or calls `add_event` from an unexpected place. The resume consumer publishes no events (it reads them and writes workflow rows); it is on that test's allow-list only to build its dead-letter producer |

## Module layout

Taken from `git ls-files` for `backend/` and `scripts/` (so it is what exists now). `__init__.py` files and the `tests/` directory are omitted here; tests are described below.

```text
├── backend/
│   ├── app/
│   │   ├── agent/
│   │   │   ├── llm/
│   │   │   │   ├── base.py
│   │   │   │   ├── factory.py
│   │   │   │   ├── fake.py
│   │   │   │   └── gemini.py
│   │   │   ├── fsm.py
│   │   │   ├── history.py
│   │   │   ├── keys.py
│   │   │   ├── orchestrator.py
│   │   │   ├── prompts.py
│   │   │   ├── results_view.py
│   │   │   ├── resume.py
│   │   │   ├── store.py
│   │   │   └── tools_view.py
│   │   ├── api/
│   │   │   ├── routers/
│   │   │   │   ├── admin.py
│   │   │   │   ├── approvals.py
│   │   │   │   ├── auth.py
│   │   │   │   ├── chat.py
│   │   │   │   ├── health.py
│   │   │   │   ├── orders.py
│   │   │   │   ├── refunds.py
│   │   │   │   └── workflows.py
│   │   │   ├── deps.py
│   │   │   ├── middleware.py
│   │   │   └── tool_responses.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── jsonutil.py
│   │   │   ├── logging.py
│   │   │   ├── login_limiter.py
│   │   │   └── security.py
│   │   ├── data/
│   │   │   ├── kb/
│   │   │   │   ├── cancellations.md
│   │   │   │   ├── contact-and-support.md
│   │   │   │   ├── damaged-or-wrong-items.md
│   │   │   │   ├── internal-agent-playbook.md
│   │   │   │   ├── internal-fraud-and-escalation-notes.md
│   │   │   │   ├── payments.md
│   │   │   │   ├── refunds.md
│   │   │   │   ├── returns-policy.md
│   │   │   │   ├── shipping.md
│   │   │   │   └── warranty.md
│   │   │   └── kb_eval/
│   │   │       └── golden.yaml
│   │   ├── db/
│   │   │   ├── migrations/
│   │   │   │   ├── versions/
│   │   │   │   │   ├── 0001_initial_schema.py
│   │   │   │   │   ├── 0002_phase2_refunds_outbox_policy.py
│   │   │   │   │   ├── 0003_agent_messages_workflow.py
│   │   │   │   │   ├── 0004_knowledge_base.py
│   │   │   │   │   ├── 0005_workflow_resume_index.py
│   │   │   │   │   ├── 0006_revoked_tokens.py
│   │   │   │   │   └── 0007_refund_status_expired.py
│   │   │   │   ├── README
│   │   │   │   ├── env.py
│   │   │   │   └── script.py.mako
│   │   │   ├── models/
│   │   │   │   ├── approval.py
│   │   │   │   ├── audit_log.py
│   │   │   │   ├── enums.py
│   │   │   │   ├── kb.py
│   │   │   │   ├── message.py
│   │   │   │   ├── order.py
│   │   │   │   ├── outbox.py
│   │   │   │   ├── payment.py
│   │   │   │   ├── processed_event.py
│   │   │   │   ├── refund.py
│   │   │   │   ├── revoked_token.py
│   │   │   │   ├── ticket.py
│   │   │   │   ├── tool_call.py
│   │   │   │   ├── user.py
│   │   │   │   └── workflow.py
│   │   │   ├── base.py
│   │   │   ├── locking.py
│   │   │   ├── session.py
│   │   │   └── vector.py
│   │   ├── events/
│   │   │   ├── consumers/
│   │   │   │   ├── base.py
│   │   │   │   ├── notifications.py
│   │   │   │   └── resume.py
│   │   │   ├── admin.py
│   │   │   ├── envelope.py
│   │   │   ├── kafka.py
│   │   │   ├── outbox.py
│   │   │   ├── relay.py
│   │   │   ├── runtime.py
│   │   │   └── types.py
│   │   ├── jobs/
│   │   │   └── sweeper.py
│   │   ├── modules/
│   │   │   ├── orders/
│   │   │   │   ├── repo.py
│   │   │   │   ├── schemas.py
│   │   │   │   └── service.py
│   │   │   ├── refunds/
│   │   │   │   ├── approvals_view.py
│   │   │   │   ├── expiry.py
│   │   │   │   ├── hashing.py
│   │   │   │   ├── payments.py
│   │   │   │   ├── reconcile.py
│   │   │   │   ├── repo.py
│   │   │   │   └── service.py
│   │   │   ├── tickets/
│   │   │   │   └── service.py
│   │   │   ├── users/
│   │   │   │   ├── repo.py
│   │   │   │   ├── service.py
│   │   │   │   └── tokens.py
│   │   │   ├── audit.py
│   │   │   └── pipeline_health.py
│   │   ├── policy/
│   │   │   ├── engine.py
│   │   │   ├── rules.py
│   │   │   ├── rules.yaml
│   │   │   └── types.py
│   │   ├── rag/
│   │   │   ├── audience.py
│   │   │   ├── chunker.py
│   │   │   ├── embedder.py
│   │   │   ├── eval.py
│   │   │   ├── factory.py
│   │   │   ├── guards.py
│   │   │   ├── ingest.py
│   │   │   ├── kb.py
│   │   │   └── retriever.py
│   │   ├── tools/
│   │   │   ├── handlers/
│   │   │   │   ├── base.py
│   │   │   │   ├── knowledge.py
│   │   │   │   ├── orders.py
│   │   │   │   ├── refunds.py
│   │   │   │   └── tickets.py
│   │   │   ├── context.py
│   │   │   ├── default.py
│   │   │   ├── executor.py
│   │   │   ├── registry.py
│   │   │   └── schemas.py
│   │   └── main.py
│   ├── alembic.ini
│   └── pyproject.toml
└── scripts/
    ├── mutation/
    │   ├── README.md
    │   ├── agent.json
    │   ├── cleanup.json
    │   ├── commit2.json
    │   ├── commit3.json
    │   ├── commit4.json
    │   ├── commit5.json
    │   ├── phase5.json
    │   ├── phase6.json
    │   ├── rag.json
    │   └── run.py
    ├── demo_cleanup.py
    ├── demo_phase2.py
    ├── demo_phase3.py
    ├── demo_phase4.py
    ├── demo_phase5.py
    ├── demo_phase6.py
    ├── kafka_smoke.py
    ├── kb_eval.py
    ├── kb_ingest.py
    ├── llm_smoke.py
    ├── outbox_admin.py
    ├── reconcile_refunds.py
    └── seed.py
```

| Path | Responsibility |
|---|---|
| `app/main.py` | App factory, lifespan (engine and Redis cleanup), middleware and router wiring |
| `app/api/middleware.py` | `RequestIDMiddleware`: validates or generates `X-Request-ID`, binds it to logs, writes the access log line, answers a crash with a clean `500` that names the id. `SecurityHeadersMiddleware`: `nosniff` on every response and `Cache-Control: no-store` under `/api/` |
| `app/api/deps.py` | `get_current_user`, `require_role`, `get_redis`, `get_session` |
| `app/api/routers/` | Thin HTTP layer: `health`, `auth` (login with the rate limiter, `/logout`, `/me`), `orders`, `refunds`, `approvals` (the enriched list, approve, reject), `chat`, `workflows`, `admin` (the staff knowledge-base lookup, the pipeline snapshot) |
| `app/api/tool_responses.py` | Maps a tool outcome (status, decision, reason code) onto the HTTP response |
| `app/core/config.py` | `pydantic-settings` configuration, the production JWT-secret guard, `configured_app_env()` / `dev_only_refusal()` for the dev-only scripts |
| `app/core/login_limiter.py` | The Redis login failure counter (per email + IP, fails open) and `client_ip` (the socket peer, or the `X-Forwarded-For` entry `TRUSTED_PROXY_HOPS` from the right) |
| `app/core/security.py`, `jsonutil.py`, `logging.py` | argon2 and JWT (HS256 pinned); JSON-safe/capped serialisation; structlog |
| `app/db/base.py`, `session.py`, `locking.py` | Declarative base and mixins; async engine and session; the row-lock helpers and **the** lock order |
| `app/db/models/` | One file per aggregate, plus `enums.py` (VARCHAR + CHECK enums) |
| `app/db/migrations/` | Async Alembic environment; `0001` initial schema (includes `CREATE EXTENSION vector`), `0002` Phase 2 schema, `0003` Phase 3 schema, `0004` the knowledge base, `0005` the waiting-turn index, `0006` revoked tokens, `0007` the refund status `expired` |
| `app/db/vector.py` | The pgvector column type and `vector_literal()` (no extra dependency) |
| `app/policy/` | `engine.py` (pure decision functions), `rules.py` (loading and validation), `types.py`, `rules.yaml` (all thresholds) |
| `app/tools/` | `registry.py` (specs, roles, `agent_visible`), `schemas.py` (inputs), `executor.py` (the pipeline), `context.py`, `default.py` (the default registry), `handlers/` (orders, tickets, refunds, knowledge) |
| `app/agent/` | `orchestrator.py` (a turn: the tool loop, caps, replay, failure handling), `fsm.py` (states and legal transitions), `keys.py` (derived idempotency keys), `tools_view.py` (what the model sees, plus the Gemini schema mapping), `results_view.py` (allow-listed, capped tool results), `history.py`, `prompts.py`, `store.py` (messages and checkpoints), `resume.py` (`finish_waiting`, `waiting_for`, the read-time fallback), `llm/` (`base.py` interface, `gemini.py`, `fake.py`, `factory.py`) |
| `app/rag/` | `embedder.py` (interface, fake, Gemini), `factory.py`, `kb.py` (load and render documents, the number-consistency check), `chunker.py`, `ingest.py`, `retriever.py`, `audience.py` (the one place that maps a role to audiences), `guards.py`, `eval.py`; the data is in `app/data/kb/` and `app/data/kb_eval/` |
| `app/modules/orders/`, `users/`, `tickets/` | Service and repo layers. Authorisation scoping lives in the orders repo/service |
| `app/modules/refunds/` | `service.py` (facts, events), `repo.py` (idempotent insert), `hashing.py` (canonical request hash), `payments.py` (provider interface, simulated provider, payout phases), `reconcile.py`, `approvals_view.py` (the read-only view and ordering for the admin list: pending oldest first, the decided tabs newest decision first), `expiry.py` (`expire_one` under the lock order, the deadline and the overdue check the decision handlers use) |
| `app/modules/users/tokens.py` | `is_revoked`, `revoke`, `purge_expired` over `revoked_tokens` |
| `app/modules/pipeline_health.py` | The read-only snapshot behind `GET /admin/pipeline` (counts and ages; the outbox through the ORM model, because only the relay may touch that table in SQL) |
| `app/jobs/sweeper.py` | The sweeper process: expire stale approvals (one per transaction), then purge expired revocations; `--once` has a deadline |
| `app/modules/audit.py` | Writes `audit_logs` rows |
| `app/events/` | `outbox.py` (writer, field allow-list), `envelope.py` (version 1), `relay.py`, `consumers/` (base class, notifications, resume), `kafka.py` (producer and consumer wrappers), `runtime.py` (signals, `--once` deadline), `admin.py` (unpark), `types.py` |
| `scripts/` | `seed.py`; `demo_phase2.py`, `demo_phase3.py`, `demo_phase4.py`, `demo_phase5.py`, `demo_phase6.py` and `demo_cleanup.py` (dev only); `kb_ingest.py` and `kb_eval.py` (the knowledge base); `reconcile_refunds.py`, `outbox_admin.py`, `kafka_smoke.py`, `llm_smoke.py` (two real calls to Gemini with your own key); `mutation/` (runner and nine lists, 116 mutations) |
| `frontend/` | Next.js 16.4.0, React 19.3.0, TypeScript 7.0.2, Tailwind: two pages (login and chat at `/`, the admin approvals page at `/admin`), a shared auth helper (`lib/auth.ts`), a same-origin proxy to the API (no CORS), an error boundary; no business logic |

Tests are in `backend/app/tests/`: **1307 tests** (`pytest --collect-only -q`) in 71 `test_*.py` files, plus shared helpers (`conftest.py` with the real-Postgres fixtures and the `committed_db` fixture for concurrency tests, `factories.py`, `tokens.py`, `policy_helpers.py`, `event_helpers.py`, `worlds.py` for building a customer/admin/order scenario, `agent_helpers.py` for an orchestrator wired to the real executor, and a golden snapshot of the Gemini tool declarations). 31 of them use the real Redpanda and carry the `kafka` marker; `pytest -m "not kafka"` runs the other 1276 with Postgres (and, for the limiter's own tests, Redis). Phase 5 added `resume_helpers.py` and seven test files (65 tests): `test_migration_0005`, `test_resume_workflow`, `test_resume_race`, `test_resume_consumer`, `test_resume_consumer_kafka` (the two Redpanda-backed ones), `test_admin_api` and `test_staff_kb_search`. Phase 6 added ten test files (135 tests): `test_login_rate_limit`, `test_token_revocation`, `test_logout`, `test_migration_0006`, `test_migration_0007`, `test_refund_expired_status`, `test_approval_expiry`, `test_admin_expiry_view`, `test_observability` and `test_pipeline_health`; `conftest.py` gives every test its own login limiter and Redis key prefix. The polish and security pass added `test_admin_list_order` (7) and `test_api_security_headers` (21).

**Planned but not created yet:** polish (Phase 7), and the answer-level evals and a re-ranker the earlier plan named; metrics, dashboards and alerting (the pipeline endpoint is read on demand). `CLAUDE.md` lists `answerer` under `rag/`; there is none, because the assistant model writes the answer from the retrieved excerpts.
