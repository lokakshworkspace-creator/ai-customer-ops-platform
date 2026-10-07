"""The tool executor: golden rule 2, in one place.

    log start -> 1 validate -> 2 authorize -> 3 policy -> 4 execute -> (5 after-commit) -> log end

Every call, however it ends, leaves a `tool_calls` row (written and COMMITTED before anything
else, so even a crash mid-pipeline leaves a trace) and an `audit_logs` row.

Transactions: the start row (T0) is committed on its own. Steps 3 and 4 share one transaction
(T1) so the row locks taken while deciding protect the writes that follow; T1 also records the
verdict and the audit row, so they commit atomically with the effect. A denied or failed call
rolls T1 back and records the outcome in a fresh transaction. `after_commit` (external side
effects) runs after T1 has committed.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jsonutil import cap_json
from app.db.models import AuditLog, ToolCall
from app.db.models.enums import PolicyDecision, ToolCallStatus
from app.db.models.tool_call import MAX_ARGUMENTS_BYTES, MAX_RESULT_BYTES
from app.policy.rules import Rules, get_rules
from app.policy.types import PolicyResult, ReasonCode
from app.tools.context import ToolContext
from app.tools.handlers.base import ToolError, ToolResult
from app.tools.registry import ToolRegistry, ToolSpec

log = structlog.get_logger(__name__)

MAX_ERROR_CHARS = 1000


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    tool_call_id: uuid.UUID
    tool_name: str
    status: ToolCallStatus
    http_status: int
    decision: PolicyDecision | None = None
    reason_code: str | None = None
    reason: str | None = None
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None
    details: list[dict[str, str]] | None = None  # validation problems, safe to show


def _clip(text: str) -> str:
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def _summarise_validation(exc: ValidationError) -> list[dict[str, str]]:
    # Deliberately omits the offending `input` values: they are attacker-controlled.
    return [
        {
            "field": ".".join(str(p) for p in e["loc"]) or "(root)",
            "message": e["msg"],
            "type": e["type"],
        }
        for e in exc.errors()[:20]
    ]


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, *, rules_provider: Callable[[], Rules] = get_rules
    ) -> None:
        self.registry = registry
        self._rules_provider = rules_provider

    async def run(
        self, session: AsyncSession, ctx: ToolContext, tool_name: str, raw_args: Any
    ) -> ToolOutcome:
        """Run one tool call. `session` must have no uncommitted writes: this method commits."""
        if session.new or session.dirty or session.deleted:
            raise RuntimeError("ToolExecutor.run needs a session without pending changes")

        spec = self.registry.get(tool_name)
        call_id = await self._log_start(session, ctx, tool_name, raw_args)
        ctx = replace(ctx, tool_call_id=call_id)  # handlers can link rows to this call
        blog = log.bind(
            tool=tool_name,
            tool_call_id=str(call_id),
            request_id=ctx.request_id,
            user_id=str(ctx.actor.id),
        )

        # -- 1. validate ------------------------------------------------------------------
        if spec is None:
            return await self._conclude_without_policy(
                session,
                ctx,
                call_id,
                tool_name,
                ToolCallStatus.invalid,
                404,
                error_code="UNKNOWN_TOOL",
                error=f"unknown tool {tool_name[:100]!r}",
                blog=blog,
            )
        try:
            args = spec.input_model.model_validate({} if raw_args is None else raw_args)
        except ValidationError as exc:
            problems = _summarise_validation(exc)
            summary = "; ".join(f"{p['field']}: {p['message']}" for p in problems)
            return await self._conclude_without_policy(
                session,
                ctx,
                call_id,
                tool_name,
                ToolCallStatus.invalid,
                422,
                error_code="INVALID_ARGUMENTS",
                error=f"invalid arguments: {summary}",
                details=problems,
                blog=blog,
            )

        # -- 2. authorize -----------------------------------------------------------------
        if ctx.actor.role not in spec.required_roles:
            roles = ", ".join(sorted(r.value for r in spec.required_roles))
            denial = PolicyResult(
                PolicyDecision.DENY,
                ReasonCode.ROLE_NOT_PERMITTED,
                f"'{tool_name}' requires one of these roles: {roles}.",
                (ReasonCode.ROLE_NOT_PERMITTED,),
            )
            return await self._conclude_denied(session, ctx, call_id, spec, denial, blog)

        # -- 3. policy + 4. execute (one transaction) -------------------------------------
        rules = self._rules_provider()
        try:
            decision = await spec.handler.decide(session, ctx, args, rules)
            policy = decision.policy
            if policy.is_deny:
                await session.rollback()  # nothing was written; release the locks now
                return await self._conclude_denied(session, ctx, call_id, spec, policy, blog)

            result = await spec.handler.execute(session, ctx, args, decision)
            deferred = result.after_commit is not None
            status = (
                ToolCallStatus.pending  # stays in flight until the after-commit step finishes
                if deferred
                else ToolCallStatus.awaiting_approval
                if policy.needs_approval
                else ToolCallStatus.succeeded
            )
            await self._finish(
                session, ctx, call_id, tool_name, status=status, policy=policy, data=result.data
            )
            await session.commit()
        except ToolError as exc:
            await session.rollback()
            return await self._conclude_without_policy(
                session,
                ctx,
                call_id,
                tool_name,
                ToolCallStatus.failed,
                exc.http_status,
                error_code=exc.code,
                error=exc.message,
                blog=blog,
            )
        except Exception as exc:
            await session.rollback()
            blog.exception("tool_internal_error")
            return await self._conclude_without_policy(
                session,
                ctx,
                call_id,
                tool_name,
                ToolCallStatus.failed,
                500,
                error_code="INTERNAL_ERROR",
                error="internal error",
                blog=blog,
                internal=f"{type(exc).__name__}: {exc}",
            )

        data = dict(result.data)
        final_status, error_code, error = status, None, None
        http_status = result.http_status

        # -- 5. after commit (external side effects) --------------------------------------
        if result.after_commit is not None:
            try:
                after = await result.after_commit(session)
                data.update(after.data)
                final_status, error_code, error = after.status, after.error_code, after.error
                if after.http_status is not None:
                    http_status = after.http_status
            except Exception as exc:
                blog.exception("tool_after_commit_error")
                await session.rollback()  # the session may be mid-failed-transaction
                final_status, error_code, error = (
                    ToolCallStatus.failed,
                    "POST_COMMIT_ERROR",
                    _clip(f"{type(exc).__name__}: {exc}"),
                )
            await self._update_final(session, call_id, final_status, data, error_code, error)
            await session.commit()

        blog.info(
            "tool_call",
            status=final_status.value,
            decision=policy.decision.value,
            reason_code=policy.reason_code.value,
        )
        return ToolOutcome(
            tool_call_id=call_id,
            tool_name=tool_name,
            status=final_status,
            http_status=http_status,
            decision=policy.decision,
            reason_code=policy.reason_code.value,
            reason=policy.reason,
            data=data,
            error_code=error_code,
            error=error,
        )

    # -- logging helpers -------------------------------------------------------------------

    async def _log_start(
        self, session: AsyncSession, ctx: ToolContext, tool_name: str, raw_args: Any
    ) -> uuid.UUID:
        arguments = raw_args if isinstance(raw_args, dict) else {"_raw": raw_args}
        call = ToolCall(
            user_id=ctx.actor.id,
            workflow_run_id=ctx.workflow_run_id,
            tool_name=tool_name[:100],
            arguments=cap_json(arguments, MAX_ARGUMENTS_BYTES),
            status=ToolCallStatus.pending,
            request_id=ctx.request_id,
        )
        session.add(call)
        await session.commit()  # T0: the trace exists before anything can go wrong
        return call.id

    async def _finish(
        self,
        session: AsyncSession,
        ctx: ToolContext,
        call_id: uuid.UUID,
        tool_name: str,
        *,
        status: ToolCallStatus,
        policy: PolicyResult | None = None,
        data: dict[str, Any] | None = None,
        error_code: str | None = None,
        error: str | None = None,
    ) -> None:
        """UPDATE the tool_calls row and INSERT the audit row. Does not commit."""
        finished = status != ToolCallStatus.pending
        await session.execute(
            update(ToolCall)
            .where(ToolCall.id == call_id)
            .values(
                status=status,
                policy_decision=policy.decision if policy else None,
                policy_reason_code=policy.reason_code.value if policy else None,
                policy_reason=policy.reason if policy else None,
                result=cap_json(data, MAX_RESULT_BYTES) if data is not None else None,
                error_code=error_code,
                error=_clip(error) if error else None,
                finished_at=func.now() if finished else None,
            )
        )
        session.add(
            AuditLog(
                actor_user_id=ctx.actor.id,
                action=f"tool.{tool_name[:90]}",
                resource_type="tool_call",
                resource_id=str(call_id),
                decision=policy.decision if policy else None,
                reason_code=(policy.reason_code.value if policy else error_code),
                reason=(policy.reason if policy else error),
                details={
                    "status": status.value,
                    "all_reason_codes": [c.value for c in policy.all_reason_codes]
                    if policy
                    else [],
                    **({"policy_details": policy.details} if policy and policy.details else {}),
                },
                request_id=ctx.request_id,
            )
        )

    async def _update_final(
        self,
        session: AsyncSession,
        call_id: uuid.UUID,
        status: ToolCallStatus,
        data: dict[str, Any],
        error_code: str | None,
        error: str | None,
    ) -> None:
        await session.execute(
            update(ToolCall)
            .where(ToolCall.id == call_id)
            .values(
                status=status,
                result=cap_json(data, MAX_RESULT_BYTES),
                error_code=error_code,
                error=_clip(error) if error else None,
                finished_at=func.now(),
            )
        )

    async def _conclude_denied(
        self,
        session: AsyncSession,
        ctx: ToolContext,
        call_id: uuid.UUID,
        spec: ToolSpec,
        policy: PolicyResult,
        blog: Any,
    ) -> ToolOutcome:
        await self._finish(
            session, ctx, call_id, spec.name, status=ToolCallStatus.denied, policy=policy
        )
        await session.commit()
        blog.info(
            "tool_call", status="denied", decision="DENY", reason_code=policy.reason_code.value
        )
        return ToolOutcome(
            tool_call_id=call_id,
            tool_name=spec.name,
            status=ToolCallStatus.denied,
            http_status=403,
            decision=PolicyDecision.DENY,
            reason_code=policy.reason_code.value,
            reason=policy.reason,
            data=None,
        )

    async def _conclude_without_policy(
        self,
        session: AsyncSession,
        ctx: ToolContext,
        call_id: uuid.UUID,
        tool_name: str,
        status: ToolCallStatus,
        http_status: int,
        *,
        error_code: str,
        error: str,
        blog: Any,
        details: list[dict[str, str]] | None = None,
        internal: str | None = None,
    ) -> ToolOutcome:
        stored = f"{error} [{internal}]" if internal else error  # internals go to the log row only
        await self._finish(
            session, ctx, call_id, tool_name, status=status, error_code=error_code, error=stored
        )
        await session.commit()
        blog.info("tool_call", status=status.value, error_code=error_code)
        return ToolOutcome(
            tool_call_id=call_id,
            tool_name=tool_name,
            status=status,
            http_status=http_status,
            error_code=error_code,
            error=error,
            details=details,
        )


__all__ = ["ToolExecutor", "ToolOutcome", "ToolResult"]
