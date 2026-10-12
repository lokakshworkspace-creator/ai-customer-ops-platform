from enum import StrEnum


class EventType(StrEnum):
    REFUND_REQUESTED = "refund.requested"
    REFUND_APPROVED = "refund.approved"
    REFUND_COMPLETED = "refund.completed"
    REFUND_FAILED = "refund.failed"
    REFUND_REJECTED = "refund.rejected"
    REFUND_EXPIRED = "refund.expired"
    TICKET_CREATED = "ticket.created"


class AggregateType(StrEnum):
    REFUND = "refund"
    TICKET = "ticket"


ENVELOPE_VERSION = 1

_REFUND_BASE = frozenset(
    {
        "refund_id",
        "order_id",
        "customer_id",
        "requested_by",
        "amount",
        "currency",
        "reason_code",
        "status",
    }
)

# The ONLY fields an event may carry. Events hold ids and state, never personal data or free text
# (no names, emails, subjects, notes, provider messages). Adding a field here is a deliberate,
# reviewed schema change; `add_event` rejects anything else when the event is WRITTEN.
EVENT_DATA_FIELDS: dict[EventType, frozenset[str]] = {
    EventType.REFUND_REQUESTED: _REFUND_BASE,
    EventType.REFUND_APPROVED: _REFUND_BASE | {"approved_by", "auto"},
    EventType.REFUND_COMPLETED: _REFUND_BASE | {"provider_ref"},
    EventType.REFUND_FAILED: _REFUND_BASE | {"failure_code"},
    EventType.REFUND_REJECTED: _REFUND_BASE | {"rejected_by"},
    EventType.REFUND_EXPIRED: _REFUND_BASE,  # nobody decided: there is no actor to name
    EventType.TICKET_CREATED: frozenset({"ticket_id", "user_id", "order_id", "priority"}),
}


def topic_for(aggregate_type: str, prefix: str = "ops") -> str:
    """One topic per aggregate type; the aggregate id is the Kafka key, so all events of one
    refund land in one partition, in order."""
    return f"{prefix}.{aggregate_type}s"


def event_topics(prefix: str = "ops") -> list[str]:
    return [topic_for(a.value, prefix) for a in AggregateType]


def dead_letter_topic(prefix: str = "ops") -> str:
    return f"{prefix}.dead-letter"
