"""Import every model so Base.metadata is complete (Alembic autogenerate relies on this)."""

from app.db.models.approval import Approval
from app.db.models.audit_log import AuditLog
from app.db.models.kb import KbChunk, KbDocument
from app.db.models.message import Message
from app.db.models.order import Order, OrderItem
from app.db.models.outbox import OutboxEvent
from app.db.models.payment import Payment
from app.db.models.processed_event import ProcessedEvent
from app.db.models.refund import Refund
from app.db.models.revoked_token import RevokedToken
from app.db.models.ticket import Ticket
from app.db.models.tool_call import ToolCall
from app.db.models.user import User
from app.db.models.workflow import WorkflowRun

__all__ = [
    "Approval",
    "AuditLog",
    "KbChunk",
    "KbDocument",
    "Message",
    "Order",
    "OrderItem",
    "OutboxEvent",
    "Payment",
    "ProcessedEvent",
    "Refund",
    "RevokedToken",
    "Ticket",
    "ToolCall",
    "User",
    "WorkflowRun",
]
