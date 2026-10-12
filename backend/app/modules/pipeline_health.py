"""Is the background machinery doing its job? A read-only snapshot for administrators.

The platform has three background processes (the outbox relay, the workflow-resume consumer and the
sweeper) and a read-time fallback that makes the customer-facing result right even when one of
them is down. That fallback is good for customers and bad for visibility: a dead consumer is
invisible from the chat page. This module looks at the DATABASE for the symptoms of each:

* relay not publishing: unpublished outbox rows that are old, or parked rows;
* resume consumer not finishing turns: a waiting chat turn whose approval is already final
  (rejected, expired, or approved and paid or failed) and has been for longer than LAG_ATTENTION_S;
* sweeper not running: a pending approval that is past its deadline for longer than the sweeper's
  interval allows.

It returns counts and ages only: no ids, no emails, no free text. It writes nothing and is not
part of /health: a lagging relay must never make a liveness probe restart the API.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OutboxEvent

LAG_ATTENTION_S = 60  # an event or a decided turn older than this, still waiting, needs a look

# Through the ORM model, not raw SQL: only the relay may touch the outbox table in SQL (a test
# enforces it), and this only reads. Unpublished rows only, so the partial index serves it.
_OUTBOX = select(
    func.count().filter(OutboxEvent.parked_at.is_(None)).label("unpublished"),
    func.count()
    .filter(OutboxEvent.parked_at.is_(None), OutboxEvent.attempts > 0)
    .label("retrying"),
    func.count().filter(OutboxEvent.parked_at.is_not(None)).label("parked"),
    func.min(OutboxEvent.created_at).filter(OutboxEvent.parked_at.is_(None)).label("oldest"),
).where(OutboxEvent.published_at.is_(None))

_APPROVALS = text(
    """
    SELECT count(*)                                              AS pending,
           count(*) FILTER (WHERE created_at <= :deadline_cutoff) AS overdue,
           min(created_at)                                       AS oldest,
           min(created_at) FILTER (WHERE created_at <= :deadline_cutoff) AS oldest_overdue
    FROM approvals
    WHERE status = 'pending'
    """
)

# A waiting turn whose approval is FINAL: rejected, expired, or approved with the refund paid or
# failed. (Approved with the refund still processing is not final: it waits for the payout.)
# The ids in the checkpoint are written by the backend; the pattern guards the cast regardless.
_TURNS = text(
    r"""
    WITH waiting AS (
        SELECT w.created_at,
               CASE WHEN w.context ->> 'approval_id' ~ '^[0-9a-f-]{36}$'
                    THEN (w.context ->> 'approval_id')::uuid END AS approval_id,
               CASE WHEN w.context ->> 'refund_id' ~ '^[0-9a-f-]{36}$'
                    THEN (w.context ->> 'refund_id')::uuid END AS refund_id
        FROM workflow_runs w
        WHERE w.status = 'waiting_approval'
    ), judged AS (
        SELECT waiting.created_at,
               (a.status IN ('rejected', 'expired')
                OR (a.status = 'approved' AND r.status IN ('succeeded', 'failed'))) AS final,
               COALESCE(r.processed_at, a.decided_at) AS final_since
        FROM waiting
        LEFT JOIN approvals a ON a.id = waiting.approval_id
        LEFT JOIN refunds r ON r.id = waiting.refund_id
    )
    SELECT count(*)                                                        AS waiting,
           min(created_at)                                                 AS oldest,
           count(*) FILTER (WHERE final)                                   AS decided,
           count(*) FILTER (WHERE final AND final_since <= :lag_cutoff)    AS stuck,
           min(final_since) FILTER (WHERE final AND final_since <= :lag_cutoff) AS oldest_stuck
    FROM judged
    """
)


def _age(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else max(0, int((now - then).total_seconds()))


async def snapshot(
    session: AsyncSession, *, now: datetime, approval_ttl: timedelta, sweeper_interval_s: float
) -> dict[str, Any]:
    lag = timedelta(seconds=LAG_ATTENTION_S)
    outbox = (await session.execute(_OUTBOX)).one()
    approvals = (await session.execute(_APPROVALS, {"deadline_cutoff": now - approval_ttl})).one()
    turns = (await session.execute(_TURNS, {"lag_cutoff": now - lag})).one()

    # The sweeper runs every `sweeper_interval_s`: an approval overdue by clearly more than that
    # (plus the usual lag) means it is not running.
    sweeper_grace = timedelta(seconds=sweeper_interval_s * 2 + LAG_ATTENTION_S)
    sweeper_stalled = (
        approvals.oldest_overdue is not None
        and now - (approvals.oldest_overdue + approval_ttl) > sweeper_grace
    )
    outbox_age = _age(now, outbox.oldest)

    attention: list[str] = []
    if outbox_age is not None and outbox_age > LAG_ATTENTION_S:
        attention.append("outbox_lagging")  # the relay is not publishing (or Redpanda is down)
    if outbox.parked:
        attention.append("outbox_parked")  # events stopped retrying: scripts/outbox_admin.py
    if turns.stuck:
        attention.append("turns_not_finished")  # the resume consumer is not finishing turns
    if sweeper_stalled:
        attention.append("approvals_overdue")  # the sweeper is not running

    return {
        "status": "attention" if attention else "ok",
        "attention": attention,
        "generated_at": now.isoformat(),
        "outbox": {
            "unpublished": outbox.unpublished,
            "retrying": outbox.retrying,
            "parked": outbox.parked,
            "oldest_unpublished_age_s": outbox_age,
        },
        "waiting_turns": {
            "waiting": turns.waiting,
            "oldest_age_s": _age(now, turns.oldest),
            "decided": turns.decided,  # waiting although the approval is final (just decided)
            "decided_not_finished": turns.stuck,  # ... for longer than the lag threshold
            "oldest_decided_not_finished_age_s": _age(now, turns.oldest_stuck),
        },
        "approvals": {
            "pending": approvals.pending,
            "overdue": approvals.overdue,  # past the deadline and not yet expired by the sweeper
            "oldest_pending_age_s": _age(now, approvals.oldest),
        },
        "thresholds": {
            "lag_attention_s": LAG_ATTENTION_S,
            "approval_ttl_hours": approval_ttl.total_seconds() / 3600,
            "sweeper_grace_s": int(sweeper_grace.total_seconds()),
        },
    }
