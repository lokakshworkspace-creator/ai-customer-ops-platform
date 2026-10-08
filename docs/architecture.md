# Architecture

This document describes the system as designed (the target) and as built (Phases 0–3). Anything not yet built is marked **planned**. Diagrams use [Mermaid](https://mermaid.js.org/), which GitHub renders natively.

- [Target architecture](#target-architecture)
- [The tool pipeline](#the-tool-pipeline)
- [The agent](#the-agent)
- [Request flows as implemented today](#request-flows-as-implemented-today)
- [Concurrency and locking](#concurrency-and-locking)
- [Data model](#data-model)
- [Golden rules](#golden-rules)
- [Module layout](#module-layout)

## Target architecture

A **modular monolith**: one FastAPI process, with orders, refunds and tickets as modules that each have a service layer and a repository layer. Postgres is the source of truth. The LLM only ever *proposes* tool calls; every proposal goes through schema validation, authorisation and the policy engine before anything executes. Two more processes run beside the API: the **outbox relay** and the **notification consumer**.

```mermaid
flowchart LR
    classDef built fill:#d4edda,stroke:#2e7d32,color:#111
    classDef prov fill:#fff3cd,stroke:#b8860b,color:#111
    classDef planned fill:#eeeeee,stroke:#777,stroke-dasharray: 5 3,color:#111

    UI["Web UI: Next.js chat page, no business logic"]:::built
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
        APPRUI["Approval UI"]:::planned
        RAG["RAG: ingest, retriever, answerer"]:::planned
    end

    LLM["LLM: Gemini over httpx, or the built-in fake"]:::built
    PG[("PostgreSQL 16: 13 tables, source of truth")]:::built
    VEC[("pgvector extension")]:::prov
    RD[("Redis")]:::prov
    RELAY["Outbox relay (separate process)"]:::built
    CONS["Notification consumer (separate process)"]:::built
    RP[["Redpanda (Kafka API)"]]:::built

    UI -- "same-origin proxy" --> MW
    MW --> AGENT
    AGENT -- "messages" --> LLM
    MW --> AUTH --> ORD --> PG
    AUTH --> REF --> PG
    HEALTH --> PG
    HEALTH --> RD

    AGENT -- "proposes tool call, origin agent" --> TOOLS
    TOOLS --> POL
    POL -- "ALLOW" --> ORD
    POL -- "ALLOW" --> REF
    POL -- "ALLOW" --> TKT
    POL -- "REQUIRE_APPROVAL" --> REF
    TOOLS -- "tool_calls + audit_logs" --> PG
    TKT --> PG
    AGENT -- "workflow_runs checkpoints, messages" --> PG
    AGENT -.-> RAG
    RAG -.-> VEC
    TOOLS -. "cache, rate limit, idempotency fast path" .-> RD
    PG -- "outbox_events" --> RELAY
    RELAY --> RP --> CONS
    CONS -- "processed_events" --> PG
```

| Colour / line | Meaning |
|---|---|
| Green, solid | **Built** (Phases 0–3) |
| Amber | **Provisioned, not yet used** — it runs in `docker-compose.yml` or is enabled in the migration, but no application code depends on it. Redis is only pinged by `/health`; the `vector` extension is enabled but there are no vector columns. |
| Grey, dashed | **Planned** (Phases 4–7) |

The tools have two callers: the HTTP layer (the refund and approval endpoints) and the agent. Both call the same executor with the authenticated `Actor`; the agent's calls carry `origin="agent"`, which narrows what it may run (next section).

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
    W -- "approval decided: read-time sync, templated message" --> D
    E["ESCALATED: the backend opens a support ticket through the executor"]
    P -- "model unreachable or error" --> F["FAILED"]
    X -- "error" --> F
```

There is no separate classification call: the model either answers in text or proposes tools. Policy and FAQ questions are a stub until Phase 4 adds a search tool (the system prompt tells the model it cannot answer them); nothing in the machine changes when that tool arrives.

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
        O-->>UI: reply, status, tool_results built by the backend, llm
    end
```

What keeps the agent inside the rules:

* **The gate.** `ToolContext.origin` is `"agent"` for model proposals. The executor refuses any tool that is not `agent_visible` before it even reads the arguments (`TOOL_NOT_AVAILABLE`, logged as a denied call), for any actor: an administrator chatting with the assistant does not give the assistant `approve_refund`. The registry also refuses to register an admin-only tool as `agent_visible`, and the model is only shown `registry.agent_tools()`.
* **What the model is shown.** `app/agent/tools_view.py` turns each tool's Pydantic schema into something a model can satisfy: money is a decimal string (the raw schema said "number or string", and a model sends `250.0`, which our validator correctly rejects), `Optional` fields are just not required, and `reason_code` is an enum taken from `rules.yaml`. The auto-approval limit is **not** told to the model. Gemini gets a second mapping to its restricted OpenAPI-style schema subset, pinned by a golden snapshot per tool.
* **Identity and keys are never model-controlled.** The actor is the authenticated user. Tool inputs forbid unknown fields, so a model-supplied `user_id`, `role` or `idempotency_key` fails validation and is logged. The refund key is derived: `agent:<conversation_id>:<message_id>:<tool>:<hash of the canonical arguments>`, so the same message always yields the same key (a retry or a repeated proposal is a replay) and a different message is judged by policy on its own.
* **Results are data.** What goes back to the model is an allow-listed, size-capped (2 KiB) summary built by the backend (`results_view.py`): no emails, names, notes or user ids. The UI gets a separate `tool_results` list built from the same outcomes, so what the backend decided is visible even if the model words it badly.
* **Limits.** At most 5 tool calls per turn and 20 per conversation (then `ESCALATED` and one ticket), a 90 s turn deadline, 30 turns per user per hour (`429`), `max_tokens` on every call, 20 messages of history, and no database transaction open while the model thinks.
* **Failure.** If the model is unreachable before any tool ran, the turn is undone and the caller gets `503` (the same `message_id` can be sent again). If tools already ran, their effects are real, so the turn is kept, marked `failed`, and the reply says what was done.
* **Ownership.** Conversations are the set of messages sharing a server-generated `conversation_id`; every message carries its `user_id`. Another user's conversation or workflow is `404`, administrators included.
* **Waiting for a human.** A turn that created an approval ends `waiting_approval`. `GET /api/v1/workflows/{id}` (and the start of the next message) runs `resume.sync_waiting`: under a row lock it re-checks the status and, once an admin has decided, finishes the workflow with one templated message. That is a write inside a GET, safe because it is one idempotent transition (a second read finds `completed` and adds nothing). It is a stub: Phase 5 replaces it with event-driven resume.

### Which model answers

`LLM_PROVIDER` selects the client (`app/agent/llm/factory.py`). `fake` (the default) is a built-in rule-based model that is deliberately **gullible**: it obeys instructions found in user text or tool results, even for tools it was not offered, so it exercises every backend defence. All tests and the demo use it (`conftest.py` forces `LLM_PROVIDER=fake` and blanks the key); outside `APP_ENV=dev` it is refused. `gemini` is `GeminiLLMClient`: one `POST` per model call to the Gemini REST API (`generateContent`, v1beta) over plain `httpx`, the key in the `x-goog-api-key` header only, a timeout and one retry for transient errors, `functionCall` / `functionResponse` parts, and `thoughtSignature` echoed back unchanged. The model name comes from `LLM_MODEL`.

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

Details, failure handling and the event schema are in [`events.md`](events.md). `relay --once` and `consumers.notifications --once` have a hard overall deadline (default 120 s; exit code 3 with a message naming what they were waiting on) so a hang is a visible failure.

## Concurrency and locking

* **One lock order, defined in one file** (`app/db/locking.py`): **order → user → refund → approval**. Anything that takes more than one of these takes them in that order, so transactions can wait on each other but never in a cycle. The locks are `FOR NO KEY UPDATE`, which conflict with each other but not with the `FOR KEY SHARE` locks foreign-key checks take.
* **The order lock** serialises refunds and cancellations on one order, so "remaining refundable" cannot be over-committed. **The user lock** serialises the per-customer velocity count across their different orders.
* **Idempotency is decided by the database.** `UNIQUE(requested_by, idempotency_key)` plus `INSERT … ON CONFLICT DO NOTHING`: ten identical concurrent requests produce exactly one refund row and one provider ledger entry (a test fires ten at once). A request hash separates "same key, same payload" (replay) from "same key, different payload" (409).
* **No external call inside a transaction or under a lock.** The provider call is made between two short transactions, and its idempotency key is `refund-<refund uuid>`, never the caller's key. An answer that never arrives leaves the refund `processing`; `scripts/reconcile_refunds.py` re-checks those.
* **Chat turns are serialised by the database too.** `UNIQUE (user_id, message_id)` and `UNIQUE (conversation_id, message_id)` on `workflow_runs` decide "the same message twice" (six simultaneous identical requests make one workflow and one refund; the others replay or get `409`), and a partial unique index allows at most one `running` turn per conversation. A turn that died with its process is marked `failed` once it is older than the turn deadline plus slack. Ten different messages asking for 400.00 of a 1000.00 order at once end with exactly two refunds: the order lock still decides.
* **The relay is safe to run twice** (`SKIP LOCKED`), and the per-aggregate ordering check means a refund's events are published in order, even when a retry or a second relay is involved.

## Data model

Thirteen tables, created by the Alembic migrations `0001` (the eleven Phase 1 tables), `0002` (Phase 2: per-user refund idempotency and `request_hash`, new refund and tool-call states, outbox ordering and parking, the `processed_events` table) and `0003` (Phase 3: the `messages` table, and conversation / message identity on `workflow_runs` plus the `escalated` status). Generated from the SQLAlchemy metadata (`Base.metadata`), so it matches the code. Types are simplified: `varchar` columns holding a fixed set of values (`status`, `role`, `decision`, …) are enforced by a named `CHECK` constraint; every money column is `numeric(12,2)` next to a `currency` column; all primary keys are UUIDs generated by the application.

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
| `refunds`, `approvals` | Written and read by the refund and approval tools |
| `tool_calls`, `audit_logs` | Written by the executor for **every** call, including denied and invalid ones |
| `outbox_events` | Written in the same transaction as each refund or ticket change; read and updated by the relay and `outbox_admin.py` |
| `processed_events` | Written by the consumer (the de-duplication ledger) |
| `tickets` | Written by the `create_ticket` tool (no HTTP route yet) |
| `workflow_runs` | One row per chat turn: state and context (the checkpoint), conversation and message ids, the frozen response; written and read by the orchestrator and `GET /workflows/{id}` |
| `messages` | The chat transcript (user, assistant and tool messages as JSON blocks); written by the orchestrator, read to build the model's history |

Constraints that carry weight:

* `refunds`: `UNIQUE(requested_by, idempotency_key)`, `amount > 0`, `request_hash` (SHA-256 of the canonical payload).
* `outbox_events`: a unique identity column `seq` (strict publish order), `next_attempt_at`, `attempts`, `parked_at`, and partial indexes for "due and unpublished" and "earlier unpublished for this aggregate".
* `approvals`: at most one *pending* approval per refund (partial unique index).
* `tool_calls`: size caps on `arguments` and `result`.
* `processed_events`: primary key `(consumer, event_id)`, which makes a duplicate delivery a no-op.
* `workflow_runs`: `UNIQUE (conversation_id, message_id)`, `UNIQUE (user_id, message_id)`, and a partial unique index allowing one `running` turn per conversation; the `workflow_status` CHECK includes `escalated`.
* `messages`: role CHECK (`user`, `assistant`, `tool`), a 32 KiB cap on `content`, and an identity column `seq` for strict order; deleting a user that has messages is refused (`RESTRICT`).

## Golden rules

These come from `CLAUDE.md` and are never to be broken. The last column says how much of each exists today.

| # | Rule | Why | Today |
|---|---|---|---|
| 1 | The LLM proposes, the backend decides. The LLM never executes anything directly. | A model can be wrong or manipulated (prompt injection), so authority must live in deterministic code that can be tested and audited. | **Built**: every model proposal runs through the executor with `origin="agent"`; admin tools are never shown to the model and are refused if proposed; the prompt-injection tests use a model that obeys everything and prove the backend still refuses |
| 2 | Every tool call passes schema validation → authorisation → policy engine → execution → `tool_calls` log. | One mandatory chokepoint means nothing runs unchecked and every attempt, allowed or denied, leaves a record. | **Built** (`app/tools/executor.py`); tests check that denied, invalid and failed calls are logged |
| 3 | Retrieved (RAG) text is data, never instructions. `rules.yaml` always beats any document. | Documents can contain injected instructions; business rules must come from one reviewed source. | `rules.yaml` is the only source of thresholds. RAG is Phase 4 |
| 4 | Postgres is the source of truth, including workflow state. Redis is disposable (cache, rate limit, idempotency fast path). | Losing Redis must never lose state or change an outcome, and a crashed workflow must be resumable from its last checkpoint. | Postgres holds all state, and idempotency does not use Redis at all. The agent's state machine is checkpointed in `workflow_runs` (state + context) and the transcript is in `messages`; Redis is provisioned but unused |
| 5 | The frontend contains zero business logic. | A single enforcement point in the backend can't be bypassed or spoofed by modifying a client. | The Next.js page only sends the message and shows the reply, the backend-built tool outcomes and the waiting state; every decision is the API's |
| 6 | Refunds are idempotent: `Idempotency-Key` plus a database `UNIQUE` constraint. | Retries, double-clicks and repeated LLM proposals must never pay out twice; the database is the final arbiter even if Redis is wrong. | **Built and concurrency-tested** (10 simultaneous identical requests make one refund and one payout). Through the agent the key is derived by the backend, and a client `message_id` makes a retried chat message a replay |
| 7 | Events go through a transactional outbox, never published directly from request handlers. | The state change and its event commit atomically, so a broker outage can neither lose an event nor emit one for a rolled-back change. | **Built**: outbox writer, relay, consumer. A test (`test_no_direct_publish.py`) scans the source and fails if anything other than the event modules imports a Kafka client, builds an `OutboxEvent` by hand, or calls `add_event` from an unexpected place |

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
│   │   │   └── security.py
│   │   ├── db/
│   │   │   ├── migrations/
│   │   │   │   ├── versions/
│   │   │   │   │   ├── 0001_initial_schema.py
│   │   │   │   │   ├── 0002_phase2_refunds_outbox_policy.py
│   │   │   │   │   └── 0003_agent_messages_workflow.py
│   │   │   │   ├── README
│   │   │   │   ├── env.py
│   │   │   │   └── script.py.mako
│   │   │   ├── models/
│   │   │   │   ├── approval.py
│   │   │   │   ├── audit_log.py
│   │   │   │   ├── enums.py
│   │   │   │   ├── message.py
│   │   │   │   ├── order.py
│   │   │   │   ├── outbox.py
│   │   │   │   ├── payment.py
│   │   │   │   ├── processed_event.py
│   │   │   │   ├── refund.py
│   │   │   │   ├── ticket.py
│   │   │   │   ├── tool_call.py
│   │   │   │   ├── user.py
│   │   │   │   └── workflow.py
│   │   │   ├── base.py
│   │   │   ├── locking.py
│   │   │   └── session.py
│   │   ├── events/
│   │   │   ├── consumers/
│   │   │   │   ├── base.py
│   │   │   │   └── notifications.py
│   │   │   ├── admin.py
│   │   │   ├── envelope.py
│   │   │   ├── kafka.py
│   │   │   ├── outbox.py
│   │   │   ├── relay.py
│   │   │   ├── runtime.py
│   │   │   └── types.py
│   │   ├── modules/
│   │   │   ├── orders/
│   │   │   │   ├── repo.py
│   │   │   │   ├── schemas.py
│   │   │   │   └── service.py
│   │   │   ├── refunds/
│   │   │   │   ├── hashing.py
│   │   │   │   ├── payments.py
│   │   │   │   ├── reconcile.py
│   │   │   │   ├── repo.py
│   │   │   │   └── service.py
│   │   │   ├── tickets/
│   │   │   │   └── service.py
│   │   │   ├── users/
│   │   │   │   ├── repo.py
│   │   │   │   └── service.py
│   │   │   └── audit.py
│   │   ├── policy/
│   │   │   ├── engine.py
│   │   │   ├── rules.py
│   │   │   ├── rules.yaml
│   │   │   └── types.py
│   │   ├── tools/
│   │   │   ├── handlers/
│   │   │   │   ├── base.py
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
    │   └── run.py
    ├── demo_cleanup.py
    ├── demo_phase2.py
    ├── demo_phase3.py
    ├── kafka_smoke.py
    ├── llm_smoke.py
    ├── outbox_admin.py
    ├── reconcile_refunds.py
    └── seed.py
```

| Path | Responsibility |
|---|---|
| `app/main.py` | App factory, lifespan (engine and Redis cleanup), middleware and router wiring |
| `app/api/middleware.py` | `RequestIDMiddleware`: validates or generates `X-Request-ID`, binds it to logs, writes the access log line |
| `app/api/deps.py` | `get_current_user`, `require_role`, `get_redis`, `get_session` |
| `app/api/routers/` | Thin HTTP layer: `health`, `auth`, `orders`, `refunds`, `approvals`, `chat`, `workflows` |
| `app/api/tool_responses.py` | Maps a tool outcome (status, decision, reason code) onto the HTTP response |
| `app/core/config.py` | `pydantic-settings` configuration, the production JWT-secret guard, `configured_app_env()` / `dev_only_refusal()` for the dev-only scripts |
| `app/core/security.py`, `jsonutil.py`, `logging.py` | argon2 and JWT (HS256 pinned); JSON-safe/capped serialisation; structlog |
| `app/db/base.py`, `session.py`, `locking.py` | Declarative base and mixins; async engine and session; the row-lock helpers and **the** lock order |
| `app/db/models/` | One file per aggregate, plus `enums.py` (VARCHAR + CHECK enums) |
| `app/db/migrations/` | Async Alembic environment; `0001` initial schema (includes `CREATE EXTENSION vector`), `0002` Phase 2 schema, `0003` Phase 3 schema |
| `app/policy/` | `engine.py` (pure decision functions), `rules.py` (loading and validation), `types.py`, `rules.yaml` (all thresholds) |
| `app/tools/` | `registry.py` (specs, roles, `agent_visible`), `schemas.py` (inputs), `executor.py` (the pipeline), `context.py`, `default.py` (the default registry), `handlers/` (orders, tickets, refunds) |
| `app/agent/` | `orchestrator.py` (a turn: the tool loop, caps, replay, failure handling), `fsm.py` (states and legal transitions), `keys.py` (derived idempotency keys), `tools_view.py` (what the model sees, plus the Gemini schema mapping), `results_view.py` (allow-listed, capped tool results), `history.py`, `prompts.py`, `store.py` (messages and checkpoints), `resume.py` (the approval sync stub), `llm/` (`base.py` interface, `gemini.py`, `fake.py`, `factory.py`) |
| `app/modules/orders/`, `users/`, `tickets/` | Service and repo layers. Authorisation scoping lives in the orders repo/service |
| `app/modules/refunds/` | `service.py` (facts, events), `repo.py` (idempotent insert), `hashing.py` (canonical request hash), `payments.py` (provider interface, simulated provider, payout phases), `reconcile.py` |
| `app/modules/audit.py` | Writes `audit_logs` rows |
| `app/events/` | `outbox.py` (writer, field allow-list), `envelope.py` (version 1), `relay.py`, `consumers/` (base class + notifications), `kafka.py` (producer and consumer wrappers), `runtime.py` (signals, `--once` deadline), `admin.py` (unpark), `types.py` |
| `scripts/` | `seed.py`; `demo_phase2.py`, `demo_phase3.py` and `demo_cleanup.py` (dev only); `reconcile_refunds.py`, `outbox_admin.py`, `kafka_smoke.py`, `llm_smoke.py` (two real calls to Gemini with your own key); `mutation/` (runner and six lists, 76 mutations) |
| `frontend/` | Next.js 16.4.0, React 19.3.0, TypeScript 7.0.2, Tailwind: one page (login and chat), a same-origin proxy to the API (no CORS), an error boundary; no business logic |

Tests are in `backend/app/tests/`: **874 tests** (`pytest --collect-only -q`) in 43 test files, plus shared helpers (`conftest.py` with the real-Postgres fixtures and the `committed_db` fixture for concurrency tests, `factories.py`, `tokens.py`, `policy_helpers.py`, `event_helpers.py`, `worlds.py` for building a customer/admin/order scenario, `agent_helpers.py` for an orchestrator wired to the real executor, and a golden snapshot of the Gemini tool declarations). 28 of them use the real Redpanda and carry the `kafka` marker; `pytest -m "not kafka"` runs the other 846 with Postgres only.

**Planned but not created yet** (from the layout in `CLAUDE.md`): `app/rag/` (Phase 4), an admin approvals page and event-driven resume (Phase 5).
