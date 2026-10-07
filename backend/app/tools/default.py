"""The default tool registry for the application."""

from app.db.models.enums import Role
from app.modules.refunds.payments import PaymentProvider, get_payment_provider
from app.policy.rules import Rules
from app.tools.handlers.orders import CancelOrderHandler, GetOrderHandler, ListOrdersHandler
from app.tools.handlers.refunds import (
    ApproveRefundHandler,
    RejectRefundHandler,
    RequestRefundHandler,
)
from app.tools.handlers.tickets import CreateTicketHandler
from app.tools.registry import ToolRegistry, ToolSpec
from app.tools.schemas import (
    ApproveRefundInput,
    CancelOrderInput,
    CreateTicketInput,
    GetOrderInput,
    ListOrdersInput,
    RejectRefundInput,
    RequestRefundInput,
)

BOTH = frozenset({Role.customer, Role.admin})
ADMIN = frozenset({Role.admin})


def build_default_registry(rules: Rules, provider: PaymentProvider | None = None) -> ToolRegistry:
    provider = provider or get_payment_provider()
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
    reg.register(
        ToolSpec(
            name="request_refund",
            description=(
                "Request a refund for one of the caller's orders. Policy decides: small refunds "
                "are paid immediately, larger or unusual ones wait for human approval."
            ),
            input_model=RequestRefundInput,
            required_roles=BOTH,
            mutating=True,
            agent_visible=True,
            handler=RequestRefundHandler(provider),
        )
    )
    # Administrator actions: same pipeline, never offered to the LLM agent.
    reg.register(
        ToolSpec(
            name="approve_refund",
            description="Approve a refund that is waiting for human approval.",
            input_model=ApproveRefundInput,
            required_roles=ADMIN,
            mutating=True,
            agent_visible=False,
            handler=ApproveRefundHandler(provider),
        )
    )
    reg.register(
        ToolSpec(
            name="reject_refund",
            description="Reject a refund that is waiting for human approval.",
            input_model=RejectRefundInput,
            required_roles=ADMIN,
            mutating=True,
            agent_visible=False,
            handler=RejectRefundHandler(),
        )
    )
    return reg
