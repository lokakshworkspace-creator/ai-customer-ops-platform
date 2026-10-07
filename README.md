# Ops Platform

An AI-powered customer-operations platform. Customers ask for things like order lookups, cancellations, refunds, support tickets and FAQ answers; an LLM agent turns each request into a **proposed action**, and the **backend decides** whether that action is allowed, needs human approval, or is denied. The model never executes anything itself.

**Status: Phases 0–1 of 7 are built — the secure foundation (database, auth, owner-scoped orders API).** The agent, policy engine, refunds, RAG and UI are planned but **not built yet**; this README only describes what exists.

Stack: Python 3.12+, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2, PostgreSQL 16 + pgvector, Redis, Redpanda, pytest.

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundation: docker-compose (Postgres+pgvector, Redis, Redpanda), FastAPI skeleton, typed config, structured logging, request IDs, `/health` | **Done** |
| 1 | Core domain + auth: 11-table schema and Alembic migration, seed data, JWT login, role-based access, owner-scoped orders API, 130 tests | **Done** |
| 2 | Tools, policy engine, refunds (idempotent), outbox relay/consumers | Planned |
| 3 | LLM agent (FSM orchestration), first UI | Planned |
| 4 | RAG over policy/FAQ documents | Planned |
| 5 | Human approval flow, events | Planned |
| 6 | Resilience, observability, evals | Planned |
| 7 | Polish | Planned |

What exists today beyond the four endpoints below: tables for payments, refunds, tickets, workflow runs, approvals, tool calls, outbox events and audit logs are created by the migration, but nothing reads or writes them yet. Redis, Redpanda and the `pgvector` extension are provisioned but unused.

### API today

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health` | none | Checks Postgres and Redis; `200` or `503` |
| POST | `/api/v1/auth/login` | none | `{email, password}` → bearer token |
| GET | `/api/v1/orders` | bearer | Paginated (`limit` 1–100, default 20; `offset`), newest first. Customers see only their own orders; admins see all |
| GET | `/api/v1/orders/{order_id}` | bearer | `404` if the order doesn't exist **or belongs to someone else** |

Interactive docs: `http://127.0.0.1:8000/docs` once the API is running.

## Quick start

Prerequisites: Git, Python 3.12+ (developed on 3.13), Docker with Compose v2, and free localhost ports 5432, 6379 and 9092. Run everything from the repo root unless stated. Developed and tested on Windows; the Mac/Linux commands are the same steps with POSIX paths (see the note under them).

### Windows (PowerShell)

```powershell
Copy-Item .env.example .env
docker compose up -d postgres redis redpanda
docker compose ps                      # wait until all three show "healthy"

cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m alembic upgrade head      # create the schema
.\.venv\Scripts\python ..\scripts\seed.py           # load demo users and orders
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

In a second terminal, from `backend/`, run the tests:

```powershell
.\.venv\Scripts\python -m pytest
```

### Mac / Linux

```bash
cp .env.example .env
docker compose up -d postgres redis redpanda
docker compose ps                      # wait until all three show "healthy"

cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m alembic upgrade head
.venv/bin/python ../scripts/seed.py
.venv/bin/python -m uvicorn app.main:app --reload

# second terminal, from backend/:
.venv/bin/python -m pytest
```

The commands were run on Windows (the shell-agnostic ones — `docker compose`, `alembic`, the seed script, `uvicorn`, `curl` — also from Git Bash). The `.venv/bin/...` paths and `python3` are the standard POSIX equivalents of the Windows venv layout and are **unverified** here.

The tests need Postgres running. They create and rebuild a separate database called `ops_test` through the real Alembic migration and never touch the dev database `ops`.

### Seeded logins (local development only)

All passwords are `password123` (override with the `SEED_PASSWORD` environment variable when seeding).

| Email | Role | Orders |
|---|---|---|
| `admin@example.com` | admin | — (sees all 10) |
| `alice@example.com` | customer | 6 |
| `bob@example.com` | customer | 4 |

The seed is idempotent (re-running it changes nothing) and refuses to run when `APP_ENV=prod`. Prices are in rupees (INR).

## Try it

With the API running on `127.0.0.1:8000`. Responses below are real output from the seeded database (IDs and timestamps differ per run).

### curl (Mac/Linux, or Git Bash on Windows)

```bash
BASE=http://127.0.0.1:8000

# Log in as Alice and Bob; keep the tokens
login() { curl -s -X POST $BASE/api/v1/auth/login -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"password123\"}" | sed -E 's/.*"access_token":"([^"]+)".*/\1/'; }
ALICE=$(login alice@example.com)
BOB=$(login bob@example.com)

# Alice lists her orders (only hers, newest first)
curl -s "$BASE/api/v1/orders?limit=2" -H "Authorization: Bearer $ALICE"

# Take one of Bob's order ids...
BOB_ORDER=$(curl -s "$BASE/api/v1/orders?limit=1" -H "Authorization: Bearer $BOB" \
  | sed -E 's/^\{"items":\[\{"id":"([^"]+)".*/\1/')

# ...and try to read it as Alice: 404, identical to an order that doesn't exist
curl -s -i $BASE/api/v1/orders/$BOB_ORDER -H "Authorization: Bearer $ALICE"
curl -s -i $BASE/api/v1/orders/00000000-0000-0000-0000-000000000000 -H "Authorization: Bearer $ALICE"
```

### Python (any OS; `httpx` is installed with `.[dev]`)

```python
import httpx

c = httpx.Client(base_url="http://127.0.0.1:8000")

def token(email):
    r = c.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}

alice, bob = token("alice@example.com"), token("bob@example.com")

orders = c.get("/api/v1/orders", headers=alice).json()
print(orders["total"], "orders for Alice")             # 6

bobs_order = c.get("/api/v1/orders", headers=bob).json()["items"][0]["id"]
r = c.get(f"/api/v1/orders/{bobs_order}", headers=alice)
print(r.status_code, r.json())                          # 404 {'detail': 'Order not found'}
```

### PowerShell

```powershell
$base = "http://127.0.0.1:8000"
$body = '{"email":"alice@example.com","password":"password123"}'
$alice = (Invoke-RestMethod -Method Post "$base/api/v1/auth/login" -ContentType application/json -Body $body).access_token
(Invoke-RestMethod "$base/api/v1/orders?limit=2" -Headers @{ Authorization = "Bearer $alice" }).items | Select-Object status, total_amount, currency
```

Expected results:

```text
POST /api/v1/auth/login       -> 200 {"access_token":"eyJ...","token_type":"bearer","expires_in":1800}
GET  /api/v1/orders           -> 200 {"items":[{"id":"...","status":"pending","total_amount":"1499.00","currency":"INR",...}],"total":6,"limit":2,"offset":0}
GET  /api/v1/orders/<bob's>   -> 404 {"detail":"Order not found"}        (as Alice)
GET  /api/v1/orders/<random>  -> 404 {"detail":"Order not found"}        (identical)
GET  /api/v1/orders           -> 401 {"detail":"Not authenticated"}      (no token)
```

Money is serialised as exact decimal strings (`"1499.00"`), never floats.

## Security design

- **Passwords:** argon2id (`argon2-cffi` defaults). Hashes created with older, weaker parameters are transparently upgraded on the next successful login.
- **JWT:** HS256 is pinned in code (`algorithms=["HS256"]`; the token header's `alg` is never trusted, and `alg=none` is rejected). `exp` and `sub` are required. Lifetime defaults to 30 minutes.
- **Role comes from the database, not the token.** Tokens carry identity only (`sub`, `iat`, `exp`). On every request the user is reloaded from Postgres, so deactivating, deleting or demoting a user takes effect immediately, and a validly signed token that *claims* `role: admin` still gets customer powers (covered by tests).
- **No account enumeration on login:** unknown email, wrong password and inactive account all return the identical `401 {"detail":"Invalid email or password"}`, and an argon2 verification against a dummy hash runs for unknown emails so the work done is the same.
- **404, not 403, for other users' orders.** The owner filter is applied in the repository query itself, so another customer's order is indistinguishable from a nonexistent one (same status, body and headers) and order IDs can't be probed. List totals are scoped the same way. Only an explicit `admin` role sees everything; any other role sees only its own orders.
- **Production secret guard:** the app refuses to start when `APP_ENV` is anything other than `dev` and `JWT_SECRET` is the built-in dev default or shorter than 32 characters.
- **Request IDs are sanitised:** an inbound `X-Request-ID` is only honoured if it fully matches `^[A-Za-z0-9_-]{8,64}$` (so newlines or odd characters can't reach logs or response headers); otherwise a fresh one is generated. It is bound to every log line and echoed in the response.
- **Smaller points:** `/health` reports only `ok`/`error` per dependency (details go to logs); published Docker ports are bound to `127.0.0.1`; money is `Numeric(12,2)` with a currency column.

The security-relevant tests were validated by mutation: the authorisation, token and login code was deliberately broken, one defect at a time, to confirm the tests fail (details in the Phase 0–1 report).

## Known limitations

- **No login rate limiting or lockout yet** — brute-force protection is absent (planned with Redis in a later phase).
- **No token revocation or refresh.** A token stays valid until it expires (30 minutes). Deactivated or deleted users are blocked immediately because the user is reloaded per request, but there is no way to revoke a single token.
- **Timing equalisation is tested structurally only:** tests assert that a password verification runs for unknown emails; response times were not measured.
- The agent, policy engine, refunds, RAG and UI do not exist yet (see Status).
- No CI, HTTPS/CORS configuration or production deployment setup.
- Mac/Linux commands are unverified (see Quick start).

## Repo layout

```text
backend/            FastAPI app (app/api, app/core, app/db, app/modules, app/tests), Alembic config
scripts/seed.py     Demo data
docker-compose.yml  Postgres + pgvector, Redis, Redpanda (bound to 127.0.0.1)
.env.example        Configuration template
CLAUDE.md           Project rules and conventions
docs/               Architecture, design decisions, Phase 0–1 report
```

See [`docs/architecture.md`](docs/architecture.md), [`docs/decisions.md`](docs/decisions.md) and [`docs/phase-0-1-report.md`](docs/phase-0-1-report.md).
