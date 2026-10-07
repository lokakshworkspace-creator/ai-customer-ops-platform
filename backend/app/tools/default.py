"""The default tool registry for the application."""

from app.db.models.enums import Role
from app.policy.rules import Rules
from app.tools.handlers.orders import CancelOrderHandler, GetOrderHandler, ListOrdersHandler
from app.tools.handlers.tickets import CreateTicketHandler
from app.tools.registry import ToolRegistry, ToolSpec
from app.tools.schemas import CancelOrderInput, CreateTicketInput, GetOrderInput, ListOrdersInput

BOTH = frozenset({Role.customer, Role.admin})


def build_default_registry(rules: Rules) -> ToolRegistry:
    reg = ToolRegistry(rules)
    reg.register(
        ToolSpec(
            name="get_order",
            description="Look up one of the caller's orders by id.",
            input_model=GetOrderInput,
            required_roles=BOTH,
            mutating=False,
            agent_visible=True,
            handler=GetOrderHandler(),
        )
    )
    reg.register(
        ToolSpec(
            name="list_orders",
            description="List the caller's orders, newest first.",
            input_model=ListOrdersInput,
            required_roles=BOTH,
            mutating=False,
            agent_visible=True,
            handler=ListOrdersHandler(),
        )
    )
    reg.register(
        ToolSpec(
            name="create_ticket",
            description="Open a support ticket, optionally about one of the caller's orders.",
            input_model=CreateTicketInput,
            required_roles=BOTH,
            mutating=True,
            agent_visible=True,
            handler=CreateTicketHandler(),
        )
    )
    reg.register(
        ToolSpec(
            name="cancel_order",
            description="Cancel an unpaid order. Paid orders must be refunded instead.",
            input_model=CancelOrderInput,
            required_roles=BOTH,
            mutating=True,
            agent_visible=True,
            handler=CancelOrderHandler(),
        )
    )
    return reg
