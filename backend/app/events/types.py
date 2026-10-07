from enum import StrEnum


class EventType(StrEnum):
    REFUND_REQUESTED = "refund.requested"
    REFUND_APPROVED = "refund.approved"
    REFUND_COMPLETED = "refund.completed"
    REFUND_FAILED = "refund.failed"
    REFUND_REJECTED = "refund.rejected"
    TICKET_CREATED = "ticket.created"


class AggregateType(StrEnum):
    REFUND = "refund"
    TICKET = "ticket"


# One topic per aggregate type; the aggregate id is the Kafka key, so all events of one refund
# land in one partition, in order.
def topic_for(aggregate_type: str, prefix: str = "ops") -> str:
    return f"{prefix}.{aggregate_type}s"
