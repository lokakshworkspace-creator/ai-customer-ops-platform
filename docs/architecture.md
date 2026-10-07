# Architecture

This document describes the system as designed (the target) and as built (Phases 0–1). Anything not yet built is marked **planned**. Diagrams use [Mermaid](https://mermaid.js.org/), which GitHub renders natively.

- [Target architecture](#target-architecture)
- [Request flows as implemented today](#request-flows-as-implemented-today)
- [Data model](#data-model)
- [Golden rules](#golden-rules)
- [Module layout](#module-layout)

## Target architecture

A **modular monolith**: one FastAPI process, with orders, refunds and tickets as modules that each have a service layer and a repository layer. Postgres is the source of truth. The LLM only ever *proposes* tool calls; every proposal goes through schema validation, authorisation and the policy engine before anything executes.

```mermaid
flowchart LR
    classDef built fill:#d4edda,stroke:#2e7d32,color:#111
    classDef prov fill:#fff3cd,stroke:#b8860b,color:#111
    classDef planned fill:#eeeeee,stroke:#777,stroke-dasharray: 5 3,color:#111

    UI["Web UI (Next.js + Tailwind)"]:::planned
    HEALTH["GET /health"]:::built

    subgraph API["FastAPI modular monolith"]
        direction TB
        MW["Request-ID middleware + structured logging"]:::built
        AUTH["Auth: JWT login, get_current_user, require_role"]:::built
        ORD["Orders module: service + repo"]:::built
        REF["Refunds module (idempotent)"]:::planned
        TKT["Tickets module"]:::planned
        AGENT["Agent: FSM + LLMClient (Claude, FakeLLM for tests)"]:::planned
        TOOLS["Tools: registry, schema validation, executor"]:::planned
        POL["Policy engine + rules.yaml: ALLOW, REQUIRE_APPROVAL, DENY"]:::planned
        APPR["Human approval flow"]:::planned
        RAG["RAG: ingest, retriever, answerer"]:::planned
    end

    PG[("PostgreSQL 16: 11 tables, source of truth")]:::built
    VEC[("pgvector extension")]:::prov
    RD[("Redis")]:::prov
    RELAY["Outbox relay + consumers"]:::planned
    RP[["Redpanda (Kafka API)"]]:::prov

    UI -.-> MW
    MW --> AUTH --> ORD --> PG
    HEALTH --> PG
    HEALTH --> RD

    AGENT -. "proposes tool call" .-> TOOLS
    TOOLS -.-> POL
    POL -. "ALLOW" .-> ORD
    POL -. "ALLOW" .-> REF
    POL -. "ALLOW" .-> TKT
    POL -. "REQUIRE_APPROVAL" .-> APPR
    TOOLS -. "tool_calls + audit_logs" .-> PG
    REF -.-> PG
    TKT -.-> PG
    APPR -.-> PG
    AGENT -. "workflow_runs checkpoints" .-> PG
    AGENT -.-> RAG
    RAG -.-> VEC
    TOOLS -. "cache, rate limit, idempotency fast path" .-> RD
    PG -. "outbox_events" .-> RELAY
    RELAY -.-> RP
```

| Colour / line | Meaning |
|---|---|
| Green, solid | **Built** (Phases 0–1) |
| Amber | **Provisioned, not yet used** — it runs in `docker-compose.yml` or is enabled in the migration, but no application code depends on it. Redis is only pinged by `/health`; Redpanda has no clients; the `vector` extension is enabled but there are no vector columns. |
| Grey, dashed | **Planned** (Phases 2–7) |

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

## Data model

Eleven tables, created by the single Alembic migration `0001`. Generated from the SQLAlchemy metadata (`Base.metadata`), so it matches the code. Types are simplified: `varchar` columns holding a fixed set of values (`status`, `role`, `decision`, …) are enforced by a named `CHECK` constraint; every money column is `numeric(12,2)` next to a `currency` column; all primary keys are UUIDs generated by the application.

```mermaid
erDiagram
    approvals {
        uuid workflow_run_id FK
        uuid tool_call_id FK
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
        text reason
        jsonb details
        varchar request_id
        timestamptz created_at
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
        varchar aggregate_type
        varchar aggregate_id
        varchar event_type
        jsonb payload
        timestamptz created_at
        timestamptz published_at
        int attempts
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
    refunds {
        varchar idempotency_key UK
        uuid payment_id FK
        uuid order_id FK
        uuid requested_by FK
        numeric amount
        varchar currency
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
        text policy_reason
        jsonb result
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
    tool_calls |o--o{ approvals : "tool_call_id"
    users |o--o{ approvals : "decided_by"
    users |o--o{ audit_logs : "actor_user_id"
    users ||--o{ approvals : "requested_by"
    users ||--o{ orders : "user_id"
    users ||--o{ refunds : "requested_by"
    users ||--o{ tickets : "user_id"
    users ||--o{ tool_calls : "user_id"
    users ||--o{ workflow_runs : "user_id"
    workflow_runs |o--o{ approvals : "workflow_run_id"
    workflow_runs |o--o{ tool_calls : "workflow_run_id"
```

Which tables application code uses today:

| Table | Today |
|---|---|
| `users` | Read by login and `get_current_user`; written by login (hash upgrade) and the seed |
| `orders`, `order_items` | Read by the orders API; written by the seed |
| `payments` | Written by the seed only |
| `refunds`, `tickets`, `workflow_runs`, `approvals`, `tool_calls`, `outbox_events`, `audit_logs` | Schema and constraints only — nothing reads or writes them yet (Phase 2+) |

The one constraint that already matters for a later phase: `refunds.idempotency_key` is `NOT NULL` with a `UNIQUE` constraint, tested at the database level.

## Golden rules

These come from `CLAUDE.md` and are never to be broken. The last column says how much of each exists today.

| # | Rule | Why | Today |
|---|---|---|---|
| 1 | The LLM proposes, the backend decides. The LLM never executes anything directly. | A model can be wrong or manipulated (prompt injection), so authority must live in deterministic code that can be tested and audited. | Planned (Phases 2–3); no LLM code exists yet |
| 2 | Every tool call passes schema validation → authorisation → policy engine → execution → `tool_calls` log. | One mandatory chokepoint means nothing runs unchecked and every attempt, allowed or denied, leaves a record. | Planned (Phase 2); `tool_calls` and `audit_logs` tables exist |
| 3 | Retrieved (RAG) text is data, never instructions. `rules.yaml` always beats any document. | Documents can contain injected instructions; business rules must come from one reviewed source. | Planned (Phases 2 and 4) |
| 4 | Postgres is the source of truth, including workflow state. Redis is disposable (cache, rate limit, idempotency fast path). | Losing Redis must never lose state or change an outcome, and a crashed workflow must be resumable from its last checkpoint. | Partly: Postgres holds all state and `workflow_runs` exists; Redis is provisioned but unused |
| 5 | The frontend contains zero business logic. | A single enforcement point in the backend can't be bypassed or spoofed by modifying a client. | No frontend yet; the API already enforces authorisation server-side |
| 6 | Refunds are idempotent: `Idempotency-Key` plus a database `UNIQUE` constraint. | Retries, double-clicks and repeated LLM proposals must never pay out twice; the database is the final arbiter even if Redis is wrong. | Constraint exists and is tested; refund logic is Phase 2 |
| 7 | Events go through a transactional outbox, never published directly from request handlers. | The state change and its event commit atomically, so a broker outage can neither lose an event nor emit one for a rolled-back change. | `outbox_events` table exists; relay and consumers are Phase 2 |

## Module layout

Taken from `git ls-files` for `backend/` and `scripts/` (so it is what exists now). `__init__.py` files and the `tests/` directory are omitted here; tests are listed below.

```text
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── routers/
│   │   │   │   ├── auth.py
│   │   │   │   ├── health.py
│   │   │   │   └── orders.py
│   │   │   ├── deps.py
│   │   │   └── middleware.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── logging.py
│   │   │   └── security.py
│   │   ├── db/
│   │   │   ├── migrations/
│   │   │   │   ├── versions/
│   │   │   │   │   └── 0001_initial_schema.py
│   │   │   │   ├── README
│   │   │   │   ├── env.py
│   │   │   │   └── script.py.mako
│   │   │   ├── models/
│   │   │   │   ├── approval.py
│   │   │   │   ├── audit_log.py
│   │   │   │   ├── enums.py
│   │   │   │   ├── order.py
│   │   │   │   ├── outbox.py
│   │   │   │   ├── payment.py
│   │   │   │   ├── refund.py
│   │   │   │   ├── ticket.py
│   │   │   │   ├── tool_call.py
│   │   │   │   ├── user.py
│   │   │   │   └── workflow.py
│   │   │   ├── base.py
│   │   │   └── session.py
│   │   ├── modules/
│   │   │   ├── orders/
│   │   │   │   ├── repo.py
│   │   │   │   ├── schemas.py
│   │   │   │   └── service.py
│   │   │   └── users/
│   │   │       ├── repo.py
│   │   │       └── service.py
│   │   └── main.py
│   ├── alembic.ini
│   └── pyproject.toml
└── scripts/
    └── seed.py
```

| Path | Responsibility |
|---|---|
| `app/main.py` | App factory, lifespan (engine and Redis cleanup), middleware and router wiring |
| `app/api/middleware.py` | `RequestIDMiddleware`: validates or generates `X-Request-ID`, binds it to logs, writes the access log line |
| `app/api/deps.py` | `get_current_user`, `require_role`, `get_redis`, `get_session` |
| `app/api/routers/` | Thin HTTP layer: `health`, `auth`, `orders` |
| `app/core/config.py` | `pydantic-settings` configuration, including the production JWT-secret guard |
| `app/core/security.py` | argon2 hashing and the dummy hash, JWT create/decode (HS256 pinned) |
| `app/core/logging.py` | structlog configuration (console in dev, JSON in prod) |
| `app/db/base.py`, `session.py` | Declarative base, naming convention, UUID/timestamp mixins; async engine and session |
| `app/db/models/` | One file per aggregate, plus `enums.py` (VARCHAR + CHECK enums) |
| `app/db/migrations/` | Async Alembic environment and the `0001` migration (includes `CREATE EXTENSION vector`) |
| `app/modules/orders/`, `users/` | Service and repo layers (orders also has response schemas). Authorisation scoping lives in the orders repo/service |
| `scripts/seed.py` | Idempotent demo data (1 admin, 2 customers, 10 orders), refuses to run in prod |

Tests are in `backend/app/tests/`: `conftest.py` (real-Postgres fixtures), `factories.py`, `tokens.py` (JWT forging helpers), and `test_auth_deps.py`, `test_auth_login.py`, `test_config.py`, `test_health.py`, `test_orders_authz.py`, `test_request_id.py`, `test_schema.py`, `test_security.py`, `test_seed.py`.

**Planned but not created yet** (from the layout in `CLAUDE.md`): `app/agent/`, `app/policy/`, `app/tools/`, `app/events/`, `app/rag/`, `app/modules/refunds/`, `app/modules/tickets/`, and the Next.js frontend.
