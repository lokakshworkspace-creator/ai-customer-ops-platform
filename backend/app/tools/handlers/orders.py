"""Order tools: get_order, list_orders (read-only) and cancel_order (mutating)."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders import service as orders_service
from app.modules.orders.schemas import OrderOut
from app.policy import CancelFacts, evaluate_cancel_order, evaluate_read
from app.policy.rules import Rules
from app.tools.context import ToolContext
from app.tools.handlers.base import Decision, ToolError, ToolResult
from app.tools.schemas import CancelOrderInput, GetOrderInput, ListOrdersInput


def _order_json(order: Any) -> dict[str, Any]:
    return OrderOut.model_validate(order).model_dump(mode="json")


class GetOrderHandler:
    async def decide(
        self, session: AsyncSession, ctx: ToolContext, args: GetOrderInput, rules: Rules
    ) -> Decision:
        # Owner-scoped lookup: another customer's order is indistinguishable from a missing one.
        order = await orders_service.get_order(session, ctx.actor, args.order_id)
        if order is None:
            raise ToolError("NOT_FOUND", "Order not found", http_status=404)
        return Decision(evaluate_read(rules, ctx.actor), state=order)

    async def execute(
        self, session: AsyncSession, ctx: ToolContext, args: GetOrderInput, decision: Decision
    ) -> ToolResult:
        return ToolResult(data={"order": _order_json(decision.state)})


class ListOrdersHandler:
    async def decide(
        self, session: AsyncSession, ctx: ToolContext, args: ListOrdersInput, rules: Rules
    ) -> Decision:
        orders, total = await orders_service.list_orders(
            session, ctx.actor, limit=args.limit, offset=args.offset
        )
        return Decision(evaluate_read(rules, ctx.actor), state=(orders, total))

    async def execute(
        self, session: AsyncSession, ctx: ToolContext, args: ListOrdersInput, decision: Decision
    ) -> ToolResult:
        orders, total = decision.state
        return ToolResult(
            data={
                "orders": [_order_json(o) for o in orders],
                "total": total,
                "limit": args.limit,
                "offset": args.offset,
            }
        )


class CancelOrderHandler:
    async def decide(
        self, session: AsyncSession, ctx: ToolContext, args: CancelOrderInput, rules: Rules
    ) -> Decision:
        # Row lock: a concurrent refund or cancel on the same order waits for this transaction.
        order = await orders_service.get_order(session, ctx.actor, args.order_id, lock=True)
        if order is None:
            raise ToolError("NOT_FOUND", "Order not found", http_status=404)
        policy = evaluate_cancel_order(rules, ctx.actor, CancelFacts(order_status=order.status))
        return Decision(policy, state=order)

    async def execute(
        self, session: AsyncSession, ctx: ToolContext, args: CancelOrderInput, decision: Decision
    ) -> ToolResult:
        order = decision.state
        await orders_service.cancel_order(session, order)
        return ToolResult(data={"order_id": str(order.id), "status": order.status.value})
