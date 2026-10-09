"""The default tool registry for the application."""

from app.db.models.enums import Role
from app.modules.refunds.payments import PaymentProvider, get_payment_provider
from app.policy.rules import Rules
from app.rag.embedder import Embedder
from app.tools.handlers.knowledge import SearchKnowledgeBaseHandler
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
    SearchKnowledgeBaseInput,
)

BOTH = frozenset({Role.customer, Role.admin})
ADMIN = frozenset({Role.admin})


def build_default_registry(
    rules: Rules, provider: PaymentProvider | None = None, embedder: Embedder | None = None
) -> ToolRegistry:
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
            name="search_knowledge_base",
            description=(
                "Search the shop's help articles for policies and FAQs (shipping, returns, "
                "refunds, payments, warranty, support hours). Use it for questions about how the "
                "shop works. It returns excerpts to quote or summarise; it cannot change anything."
            ),
            input_model=SearchKnowledgeBaseInput,
            required_roles=BOTH,
            mutating=False,
            agent_visible=True,
            handler=SearchKnowledgeBaseHandler(embedder),
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
            description=(
                "Cancel an unpaid order. Paid orders must be refunded instead. This changes the "
                "order: call it only after the customer has explicitly agreed to the cancellation."
            ),
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
                "are paid immediately, larger or unusual ones wait for human approval. Only call "
                "it once the customer has given the reason (or it is clearly implied); ask them "
                "otherwise, never choose a reason yourself. Unpaid (pending) orders cannot be "
                "refunded: offer to cancel them instead."
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
