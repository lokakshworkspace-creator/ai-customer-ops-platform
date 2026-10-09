"""The agent orchestrator: ONE chat message ("turn") = one tool loop = one `workflow_runs` row.

Golden rule 1: the model PROPOSES. Every proposal goes through `ToolExecutor.run` (validate ->
authorize -> policy -> execute -> log) with `origin="agent"`. What the orchestrator adds:

* identity is the authenticated `Actor`; the model supplies none (tool schemas forbid it);
* the Idempotency-Key is derived here (keys.py), never taken from the model;
* hard caps on tool calls per turn and per conversation (ESCALATED, with a support ticket);
* a deadline for the whole turn, and NO database transaction is ever open during a model call;
* state + context are checkpointed in Postgres on every transition (fsm.py);
* the client's `message_id` makes a repeated request a REPLAY of the stored response: no model
  call, no tool call, no second refund.

Failure semantics: if the model cannot be reached before any tool ran, the turn is undone
(nothing happened) and the caller gets `AssistantUnavailable` (503); the same message_id can be
sent again. If tools already ran, their results are real, so the turn is kept, marked failed, and
the reply says what was done.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import resume, store
from app.agent.fsm import Caps, State, check_transition
from app.agent.history import trim_history
from app.agent.keys import derive_idempotency_key
from app.agent.llm.base import (
    LLMClient,
    LLMError,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.agent.llm.base import Message as LLMMessage
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.results_view import (
    awaiting_approval,
    is_error,
    sources_of,
    tool_result_text,
    ui_summary,
)
from app.agent.tools_view import agent_tool_defs
from app.core.config import Settings
from app.db.models.enums import MessageRole, WorkflowStatus
from app.policy.rules import Rules, get_rules
from app.policy.types import Actor
from app.tools.context import ToolContext
from app.tools.executor import ToolExecutor, ToolOutcome

log = structlog.get_logger(__name__)

STALE_SLACK_S = 30.0
APPROVAL_NOTE = (
    "\n\nThis refund needs a person to approve it. I will show the result here once it is decided."
)
LIMIT_REPLY = (
    "I have reached my limit for this request, so I have passed it to our support team"
    "{ticket}. A person will follow up."
)


class AgentError(Exception):
    http_status = 400
    code = "AGENT_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConversationNotFound(AgentError):
    http_status, code = 404, "CONVERSATION_NOT_FOUND"


class TurnInProgress(AgentError):
    http_status, code = 409, "TURN_IN_PROGRESS"


class MessageIdReused(AgentError):
    http_status, code = 409, "MESSAGE_ID_REUSED"


class TurnBudgetExceeded(AgentError):
    http_status, code = 429, "TOO_MANY_TURNS"


class AssistantUnavailable(AgentError):
    http_status, code = 503, "ASSISTANT_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ChatResult:
    response: dict[str, Any]
    replayed: bool = False


def make_response(
    *,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    message_id: uuid.UUID,
    status: WorkflowStatus,
    reply: str,
    tool_results: list[dict[str, Any]],
    llm: str,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "conversation_id": str(conversation_id),
        "workflow_id": str(run_id),
        "message_id": str(message_id),
        "status": status.value,
        "reply": reply,
        "tool_results": tool_results,
        "sources": sources or [],
        "llm": llm,
    }


class Orchestrator:
    def __init__(
        self,
        executor: ToolExecutor,
        llm: LLMClient,
        settings: Settings,
        *,
        rules_provider=get_rules,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self.executor = executor
        self.llm = llm
        self.settings = settings
        self._rules_provider = rules_provider
        self._clock = clock

    @property
    def rules(self) -> Rules:
        return self._rules_provider()

    # ------------------------------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------------------------------

    async def handle_message(
        self,
        session: AsyncSession,
        *,
        actor: Actor,
        request_id: str | None,
        text: str,
        message_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
    ) -> ChatResult:
        s = self.settings

        # 1. The same message_id again: replay the stored response. No model, no tools.
        existing = await store.find_run(session, actor.id, message_id)
        if existing is not None:
            return await self._replay(session, existing, conversation_id)

        # 2. The conversation must be this user's own; a new one is generated by the server.
        if conversation_id is not None:
            if await store.conversation_owner(session, conversation_id) != actor.id:
                raise ConversationNotFound("Conversation not found")
            await resume.sync_conversation(session, conversation_id)
        else:
            conversation_id = uuid.uuid4()

        # 3. Each turn costs real LLM money: a per-user budget (no Redis rate limit yet).
        since = self._clock() - timedelta(hours=1)
        if await store.recent_turns(session, actor.id, since) >= s.agent_turns_per_hour:
            await session.rollback()
            raise TurnBudgetExceeded(
                f"Too many messages this hour (limit {s.agent_turns_per_hour}). Please try later."
            )

        # 4. Create the run and store the user's message (one transaction). The unique indexes
        #    arbitrate a race with the same message_id or with another turn in flight.
        try:
            await store.abandon_stale_runs(
                session, conversation_id, s.agent_turn_deadline_s + STALE_SLACK_S
            )
            run_id = store.new_run(
                session,
                user_id=actor.id,
                conversation_id=conversation_id,
                message_id=message_id,
                context={"calls": [], "tool_calls": 0},
            )
            await session.flush()
            store.add_message(
                session,
                conversation_id=conversation_id,
                user_id=actor.id,
                run_id=run_id,
                role=MessageRole.user,
                blocks=[TextBlock(text)],
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await store.find_run(session, actor.id, message_id)
            if existing is not None:
                return await self._replay(session, existing, conversation_id)
            raise TurnInProgress(
                "Your previous message is still being processed. Please wait a moment."
            ) from None

        # 5. Run the loop. Nothing below may keep an ORM object across a commit.
        history = await store.load_history(session, conversation_id)
        prior_calls = await store.conversation_tool_calls(session, conversation_id)
        await session.rollback()  # end the read transaction: none is open during model calls
        turn = _Turn(
            actor=actor,
            request_id=request_id,
            run_id=run_id,
            conversation_id=conversation_id,
            message_id=message_id,
            text=text,
            history=history,
            conv_calls=prior_calls,
        )
        try:
            async with asyncio.timeout(s.agent_turn_deadline_s):
                response = await self._loop(session, turn)
        except (LLMError, TimeoutError) as exc:
            return await self._failed(session, turn, exc)
        except Exception as exc:
            log.exception("agent_turn_error", workflow_id=str(run_id))
            return await self._failed(session, turn, exc)
        return ChatResult(response)

    # ------------------------------------------------------------------------------------------
    # replay
    # ------------------------------------------------------------------------------------------

    async def _replay(
        self, session: AsyncSession, run, requested_conversation: uuid.UUID | None
    ) -> ChatResult:
        if requested_conversation is not None and requested_conversation != run.conversation_id:
            await session.rollback()
            raise MessageIdReused("That message_id was already used in another conversation.")
        if run.response is not None:
            response = dict(run.response)
            await session.rollback()
            return ChatResult(response, replayed=True)
        # No stored response: the turn is in flight, or its process died before finishing.
        stale = self._clock() - run.updated_at > timedelta(
            seconds=self.settings.agent_turn_deadline_s + STALE_SLACK_S
        )
        status = run.status
        run_id, conv, msg = run.id, run.conversation_id, run.message_id
        if status == WorkflowStatus.running and not stale:
            await session.rollback()
            raise TurnInProgress("This message is still being processed.")
        reply = "That message could not be completed. Please send it again as a new message."
        response = make_response(
            conversation_id=conv,
            run_id=run_id,
            message_id=msg,
            status=WorkflowStatus.failed,
            reply=reply,
            tool_results=[],
            llm=self.llm.name,
        )
        await store.checkpoint(
            session, run_id, State.FAILED, {}, response=response, error="abandoned"
        )
        await session.commit()
        return ChatResult(response, replayed=True)

    # ------------------------------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------------------------------

    async def _loop(self, session: AsyncSession, t: "_Turn") -> dict[str, Any]:
        s = self.settings
        caps = Caps(s.agent_max_tool_calls_per_turn, s.agent_max_tool_calls_per_conversation)
        tools = agent_tool_defs(self.executor.registry, self.rules)
        t.state = State.PROPOSE
        escalate = False

        if caps.exceeded(0, t.conv_calls):  # the conversation is already out of budget
            escalate = True

        while not escalate:
            resp = await self.llm.complete(
                system=SYSTEM_PROMPT,
                messages=trim_history(t.history, s.llm_history_messages),
                tools=tools,
                max_tokens=s.llm_max_tokens,
            )
            uses = [] if resp.stop_reason == "max_tokens" else resp.tool_uses
            if not uses:
                t.reply = resp.text
                break

            blocks = [b for b in resp.content if isinstance(b, TextBlock | ToolUseBlock)]
            await self._go(session, t, State.EXECUTE)
            t.add(session, MessageRole.assistant, LLMMessage.assistant(*blocks))
            await session.commit()  # the executor needs a session with no pending changes

            results: list[ToolResultBlock] = []
            for use in uses:
                if caps.exceeded(t.turn_calls, t.conv_calls):
                    escalate = True
                    results.append(
                        ToolResultBlock(
                            use.id,
                            json.dumps(
                                {
                                    "tool": use.name,
                                    "status": "not_executed",
                                    "reason": "tool-call limit reached",
                                }
                            ),
                            is_error=True,
                        )
                    )
                    continue
                outcome = await self._execute(session, t, use)
                results.append(
                    ToolResultBlock(use.id, tool_result_text(outcome), is_error=is_error(outcome))
                )
            t.add(session, MessageRole.tool, LLMMessage.tool(*results))
            if escalate:
                break
            await self._go(session, t, State.PROPOSE)  # checkpoints and commits

        if escalate:
            return await self._escalate(session, t)
        return await self._respond(session, t)

    async def _execute(self, session: AsyncSession, t: "_Turn", use: ToolUseBlock) -> ToolOutcome:
        args = use.input if isinstance(use.input, dict) else {}
        spec = self.executor.registry.get(use.name)
        canonical: Any = args
        if spec is not None:
            try:
                canonical = spec.input_model.model_validate(args)
            except ValidationError:
                canonical = args  # the executor will reject it and log why
        ctx = ToolContext(
            actor=t.actor,
            request_id=t.request_id,
            workflow_run_id=t.run_id,
            idempotency_key=derive_idempotency_key(
                t.conversation_id, t.message_id, use.name, canonical
            ),
            origin="agent",
            now=self._clock(),
        )
        await session.commit()  # the executor needs a session with no open transaction state
        outcome = await self.executor.run(session, ctx, use.name, args)
        t.turn_calls += 1
        t.conv_calls += 1
        t.summaries.append(ui_summary(outcome))
        t.add_sources(sources_of(outcome))
        t.calls.append(
            {
                "tool": use.name,
                "status": outcome.status.value,
                "decision": outcome.decision.value if outcome.decision else None,
                "reason_code": outcome.reason_code or outcome.error_code,
                "tool_call_id": str(outcome.tool_call_id),
            }
        )
        if awaiting_approval(outcome):
            data = outcome.data or {}
            t.approval_id = data.get("approval_id")
            t.refund_id = (data.get("refund") or {}).get("id")
        return outcome

    # ------------------------------------------------------------------------------------------
    # endings
    # ------------------------------------------------------------------------------------------

    async def _respond(self, session: AsyncSession, t: "_Turn") -> dict[str, Any]:
        await self._go(session, t, State.RESPOND)
        reply = t.reply.strip() or _fallback_reply(t.summaries)
        waiting = t.approval_id is not None and t.refund_id is not None
        if waiting and "approv" not in reply.lower():
            reply += APPROVAL_NOTE
        final = State.WAITING_FOR_HUMAN if waiting else State.DONE
        check_transition(State.RESPOND, final)
        response = self._response(t, _status_of(final), reply)
        t.add(session, MessageRole.assistant, LLMMessage.assistant(TextBlock(reply)))
        ctx = t.context()
        if waiting:
            ctx["approval_id"], ctx["refund_id"] = t.approval_id, t.refund_id
        await store.checkpoint(session, t.run_id, final, ctx, response=response)
        await session.commit()
        if waiting:
            # The race: an admin may have decided between the tool call that created the approval
            # and this commit (the model's final answer takes seconds). That decision's event found
            # a workflow that was not waiting yet and did nothing. Look once now, after the commit:
            # a decision committed BEFORE this point is seen here, one committed AFTER it is
            # delivered to a workflow that now waits. finish_waiting is row-locked and idempotent,
            # so doing both is harmless, and it never calls the model or a tool.
            await resume.finish_waiting(session, t.run_id)
        return response

    async def _escalate(self, session: AsyncSession, t: "_Turn") -> dict[str, Any]:
        """A limit was hit. The BACKEND (not the model) opens one support ticket for the customer
        through the executor, so it is validated, policy-checked and logged like any other call."""
        check_transition(t.state, State.ESCALATED)
        t.state = State.ESCALATED
        ticket = ""
        if not await store.conversation_was_escalated(session, t.conversation_id):
            body = (
                "The assistant stopped because it reached its tool-call limit.\n"
                f"Conversation {t.conversation_id}, workflow {t.run_id}.\n"
                f"Customer's last message: {t.text[:500]}"
            )
            ctx = ToolContext(
                actor=t.actor,
                request_id=t.request_id,
                workflow_run_id=t.run_id,
                idempotency_key=derive_idempotency_key(
                    t.conversation_id, t.message_id, "escalation", {"conversation": "limit"}
                ),
                origin="api",  # the backend asked, not the model
                now=self._clock(),
            )
            await session.commit()
            outcome = await self.executor.run(
                session,
                ctx,
                "create_ticket",
                {"subject": "Chat assistant limit reached", "body": body, "priority": "high"},
            )
            t.summaries.append(ui_summary(outcome))
            ticket_id = ((outcome.data or {}).get("ticket") or {}).get("id")
            if ticket_id:
                ticket = f" (ticket {ticket_id})"
        reply = LIMIT_REPLY.format(ticket=ticket)
        response = self._response(t, WorkflowStatus.escalated, reply)
        t.add(session, MessageRole.assistant, LLMMessage.assistant(TextBlock(reply)))
        await store.checkpoint(session, t.run_id, State.ESCALATED, t.context(), response=response)
        await session.commit()
        return response

    async def _failed(self, session: AsyncSession, t: "_Turn", exc: BaseException) -> ChatResult:
        await session.rollback()
        executed = await store.run_has_tool_calls(session, t.run_id)
        if not executed:
            # Nothing happened: undo the turn so the same message_id can be sent again.
            await store.delete_turn(session, t.run_id)
            await session.commit()
            if isinstance(exc, LLMError):
                raise AssistantUnavailable(str(exc)) from None
            if isinstance(exc, TimeoutError):
                raise AssistantUnavailable(
                    "The assistant took too long to answer. Please try again."
                ) from None
            raise exc
        # Tools ran, so their effects are real: keep the turn and say what was done.
        done = "; ".join(s["summary"] for s in t.summaries) or "some actions"
        reply = (
            "Part of your request was carried out, but the assistant stopped before finishing. "
            f"What happened: {done}. Please check your orders before trying again."
        )
        response = self._response(t, WorkflowStatus.failed, reply)
        t.add(session, MessageRole.assistant, LLMMessage.assistant(TextBlock(reply)))
        await store.checkpoint(
            session,
            t.run_id,
            State.FAILED,
            t.context(),
            response=response,
            error=type(exc).__name__,
        )
        await session.commit()
        return ChatResult(response)

    # ------------------------------------------------------------------------------------------

    async def _go(self, session: AsyncSession, t: "_Turn", new: State) -> None:
        check_transition(t.state, new)
        t.state = new
        await store.checkpoint(session, t.run_id, new, t.context())
        await session.commit()

    def _response(self, t: "_Turn", status: WorkflowStatus, reply: str) -> dict[str, Any]:
        return make_response(
            conversation_id=t.conversation_id,
            run_id=t.run_id,
            message_id=t.message_id,
            status=status,
            reply=reply,
            tool_results=t.summaries,
            sources=t.sources,
            llm=self.llm.name,
        )


def _status_of(state: State) -> WorkflowStatus:
    from app.agent.fsm import STATUS_OF

    return STATUS_OF[state]


def _fallback_reply(summaries: list[dict[str, Any]]) -> str:
    if not summaries:
        return (
            "I am not sure how to help with that. "
            "I can look up orders, request refunds and open tickets."
        )
    return "Here is what happened: " + "; ".join(s["summary"] for s in summaries) + "."


class _Turn:
    """Mutable state of one turn. Plain data only: never an ORM object."""

    def __init__(
        self,
        *,
        actor: Actor,
        request_id: str | None,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        text: str,
        history: list[LLMMessage],
        conv_calls: int,
    ) -> None:
        self.actor = actor
        self.request_id = request_id
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.text = text
        self.history = history
        self.conv_calls = conv_calls  # tool calls in the whole conversation, so far
        self.turn_calls = 0
        self.state = State.PROPOSE
        self.reply = ""
        self.summaries: list[dict[str, Any]] = []
        self.sources: list[dict[str, Any]] = []  # documents the answer rests on (backend-computed)
        self.calls: list[dict[str, Any]] = []
        self.approval_id: str | None = None
        self.refund_id: str | None = None

    def add_sources(self, new: list[dict[str, Any]]) -> None:
        known = {s["document_id"] for s in self.sources}
        self.sources.extend(s for s in new if s["document_id"] not in known)

    def add(self, session: AsyncSession, role: MessageRole, message: LLMMessage) -> None:
        self.history.append(message)
        store.add_message(
            session,
            conversation_id=self.conversation_id,
            user_id=self.actor.id,
            run_id=self.run_id,
            role=role,
            blocks=list(message.content),
        )

    def context(self) -> dict[str, Any]:
        return {"calls": self.calls, "tool_calls": self.turn_calls}
