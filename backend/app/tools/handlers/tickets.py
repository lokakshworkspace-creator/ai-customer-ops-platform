"""create_ticket: mutating, always allowed by policy, but scoped to orders the caller can see."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders import service as orders_service
from app.modules.tickets import service as tickets_service
from app.policy import evaluate_create_ticket
from app.policy.rules import Rules
from app.tools.context import ToolContext
from app.tools.handlers.base import Decision, ToolError, ToolResult
from app.tools.schemas import CreateTicketInput


class CreateTicketHandler:
    async def decide(
        self, session: AsyncSession, ctx: ToolContext, args: CreateTicketInput, rules: Rules
    ) -> Decision:
        if args.order_id is not None:
            order = await orders_service.get_order(session, ctx.actor, args.order_id)
            if order is None:  # not yours is the same as not found
                raise ToolError("NOT_FOUND", "Order not found", http_status=404)
        return Decision(evaluate_create_ticket(rules, ctx.actor))

    async def execute(
        self, session: AsyncSession, ctx: ToolContext, args: CreateTicketInput, decision: Decision
    ) -> ToolResult:
        ticket = await tickets_service.create_ticket(
            session,
            user_id=ctx.actor.id,  # always the authenticated caller
            subject=args.subject,
            body=args.body,
            order_id=args.order_id,
            priority=args.priority,
            request_id=ctx.request_id,
        )
        return ToolResult(
            data={
                "ticket": {
                    "id": str(ticket.id),
                    "subject": ticket.subject,
                    "status": ticket.status.value,
                    "priority": ticket.priority.value,
                    "order_id": str(ticket.order_id) if ticket.order_id else None,
                }
            },
            http_status=201,
        )
