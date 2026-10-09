# AI-Powered Customer Operations Platform

An LLM agent that handles customer requests (order lookup, cancel, refund, tickets, FAQs)
by proposing actions. The backend decides whether they're allowed.

## Golden rules (never break)
1. LLM proposes, backend decides. The LLM never executes anything directly.
2. Every tool call passes: schema validation → authz → policy engine → execution → tool_calls log.
3. Retrieved (RAG) text is data, never instructions. rules.yaml always beats any document.
4. Postgres is the source of truth (incl. workflow state). Redis is disposable (cache, rate limit, idempotency fast path).
5. Frontend contains zero business logic.
6. Refunds are idempotent: Idempotency-Key + DB UNIQUE constraint.
7. Events go through a transactional outbox, never published directly from request handlers.

## Stack
FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2, Postgres 16 + pgvector, Redis,
Redpanda (Kafka-compatible), embeddings from Gemini (gemini-embedding-001, 768 dims) over httpx
(no sentence-transformers),
LLM via an `LLMClient` interface (Claude impl + FakeLLM for tests), Next.js + Tailwind (later).
Hand-rolled FSM for orchestration with Postgres checkpoints.

## Architecture
Modular monolith. Order/Refund/Ticket are modules with service + repo layers, not microservices.

backend/app/
  api/ (routers, deps, middleware) · core/ (config, security, logging) · agent/ (FSM, prompts, llm)
  policy/ (engine + rules.yaml) · tools/ (registry, schemas, executor) · modules/ (orders, refunds, tickets)
  events/ (outbox, relay, consumers) · rag/ (embedder, kb, chunker, ingest, retriever, guards, eval; documents in data/kb) · db/ (models, migrations) · tests/

## Policy outcomes
ALLOW | REQUIRE_APPROVAL | DENY, always with a reason (written to audit_logs).

## Conventions
- Type hints everywhere, ruff for lint/format, pytest for tests.
- Write tests alongside each feature. Security-relevant behavior (authz, idempotency, policy) must have tests.
- Small commits per feature. Don't build ahead of the current phase.
- Ask before adding new dependencies or changing the golden rules.
- Never drop/reset a database without asking.
- Alembic autogenerate duplicates CHECK constraints on enum columns; always hand-review new migrations so each enum has exactly one named CHECK.

## Phases
0 Foundation · 1 Core domain + auth · 2 Tools + policy + refunds (+ outbox relay/consumers)
3 Agent + LLM + first UI · 4 RAG · 5 Human approval + events · 6 Resilience, observability, evals · 7 Polish

## Current phase: Phases 0-4 done. Next: Phase 5 (admin approvals page, event-driven resume)
