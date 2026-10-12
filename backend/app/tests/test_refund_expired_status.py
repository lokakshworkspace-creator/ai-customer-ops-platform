"""`expired` is a refund status and an event type: everywhere that lists the statuses or the events
has to know it. (The migration and its effect on the rules are in test_migration_0007.py.)"""

import uuid
from decimal import Decimal

import pytest

from app.agent import resume
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.results_view import REFUND_STATUS_NOTES, ui_summary
from app.db.models import Approval, Refund
from app.db.models.enums import ApprovalStatus, PolicyDecision, RefundStatus, ToolCallStatus
from app.events.consumers.notifications import MESSAGES
from app.events.consumers.resume import RESUME_EVENTS
from app.events.outbox import add_event
from app.events.types import EVENT_DATA_FIELDS, AggregateType, EventType
from app.tools.executor import ToolOutcome


def test_every_event_type_has_a_notification_template() -> None:
    """A missing template silently logs "event <type>": no event may be added without one."""
    for event_type in EventType:
        assert event_type.value in MESSAGES, f"no notification template for {event_type.value}"
    assert "expired" in MESSAGES["refund.expired"]


def test_refund_expired_is_an_event_with_the_refund_base_fields_only() -> None:
    fields = EVENT_DATA_FIELDS[EventType.REFUND_EXPIRED]
    assert {"refund_id", "order_id", "customer_id", "status"} <= fields
    assert not {"rejected_by", "approved_by", "decided_by"} & fields  # nobody decided


def test_the_outbox_writer_accepts_the_expired_event_and_refuses_extra_fields(db_session) -> None:
    base = dict.fromkeys(EVENT_DATA_FIELDS[EventType.REFUND_EXPIRED], "x")
    add_event(
        db_session,
        aggregate_type=AggregateType.REFUND,
        aggregate_id=uuid.uuid4(),
        event_type=EventType.REFUND_EXPIRED,
        data=base,
    )
    with pytest.raises(ValueError, match="not allowed"):
        add_event(
            db_session,
            aggregate_type=AggregateType.REFUND,
            aggregate_id=uuid.uuid4(),
            event_type=EventType.REFUND_EXPIRED,
            data={**base, "note": "free text"},
        )


def test_the_resume_consumer_reacts_to_the_expired_event() -> None:
    assert EventType.REFUND_EXPIRED.value in RESUME_EVENTS


def _approval_and_refund(approval: ApprovalStatus, refund: RefundStatus):
    return (
        Approval(status=approval),
        Refund(status=refund, amount=Decimal("700.00"), currency="INR"),
    )


def test_an_expired_approval_is_final_and_says_so() -> None:
    approval, refund = _approval_and_refund(ApprovalStatus.expired, RefundStatus.expired)
    assert resume.waiting_for(approval, refund) is None
    text = resume.outcome_text(approval, refund)
    assert "expired" in text and "700.00 INR" in text
    assert "paid" not in text.replace("not paid", "")


def test_the_customer_summary_of_an_expired_refund_says_nothing_was_paid() -> None:
    outcome = ToolOutcome(
        tool_call_id=uuid.uuid4(),
        tool_name="request_refund",
        status=ToolCallStatus.succeeded,
        http_status=200,
        decision=PolicyDecision.ALLOW,
        data={"refund": {"amount": "700.00", "currency": "INR", "status": "expired"}},
    )
    summary = ui_summary(outcome)["summary"]
    assert summary.startswith("Refund of 700.00 INR: expired")
    assert REFUND_STATUS_NOTES["expired"] in summary
    assert "nothing was paid" in summary


def test_other_refund_statuses_keep_their_plain_summary() -> None:
    outcome = ToolOutcome(
        tool_call_id=uuid.uuid4(),
        tool_name="request_refund",
        status=ToolCallStatus.succeeded,
        http_status=201,
        data={"refund": {"amount": "250.00", "currency": "INR", "status": "succeeded"}},
    )
    assert ui_summary(outcome)["summary"] == "Refund of 250.00 INR: succeeded"


def test_the_assistant_is_told_what_expired_means() -> None:
    assert '"expired" means nobody decided' in SYSTEM_PROMPT
    assert "NOT paid" in SYSTEM_PROMPT
