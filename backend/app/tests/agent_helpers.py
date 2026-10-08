"""Helpers for the agent tests: an orchestrator wired to the REAL executor, registry and policy,
with a fake model and the simulated payment provider."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm.base import LLMClient
from app.agent.orchestrator import ChatResult, Orchestrator
from app.core.config import Settings
from app.modules.refunds.payments import SimulatedPaymentProvider
from app.policy.rules import get_rules
from app.policy.types import Actor
from app.tests.worlds import Person
from app.tools.default import build_default_registry
from app.tools.executor import ToolExecutor


def make_orchestrator(
    llm: LLMClient,
    provider: SimulatedPaymentProvider | None = None,
    **settings: Any,
) -> Orchestrator:
    executor = ToolExecutor(
        build_default_registry(get_rules(), provider or SimulatedPaymentProvider())
    )
    return Orchestrator(executor, llm, Settings(_env_file=None, **settings))


async def chat(
    orch: Orchestrator,
    session: AsyncSession,
    who: Person,
    text: str,
    *,
    message_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> ChatResult:
    return await orch.handle_message(
        session,
        actor=Actor(id=who.id, role=who.role),
        request_id="req-agent-test",
        text=text,
        message_id=message_id or uuid.uuid4(),
        conversation_id=conversation_id,
    )


async def count(session: AsyncSession, model: type, *where: Any) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def all_rows(session: AsyncSession, model: type, *where: Any) -> list:
    return list(
        await session.scalars(select(model).where(*where).execution_options(populate_existing=True))
    )
