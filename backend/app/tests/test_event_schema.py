"""The event envelope and the allow-list of event fields. Pure: no database, no broker."""

import re
import uuid
from pathlib import Path

import pytest

from app.events import outbox
from app.events.envelope import EventEnvelope, PoisonMessage, parse_envelope
from app.events.types import (
    ENVELOPE_VERSION,
    EVENT_DATA_FIELDS,
    AggregateType,
    EventType,
    dead_letter_topic,
    event_topics,
    topic_for,
)
from app.tests.event_helpers import refund_data, valid_envelope_bytes

DOC = Path(__file__).resolve().parents[3] / "docs" / "events.md"
PERSONAL_DATA_NAMES = {
    "email",
    "name",
    "full_name",
    "first_name",
    "last_name",
    "phone",
    "address",
    "password",
    "password_hash",
    "token",
    "subject",
    "body",
    "note",
    "message",
    "reason",
    "error",
    "card",
    "comment",
    "description",
}


class FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, row) -> None:
        self.added.append(row)


def add(event_type, data, session=None):
    return outbox.add_event(
        session or FakeSession(),
        aggregate_type=AggregateType.REFUND,
        aggregate_id=uuid.uuid4(),
        event_type=event_type,
        data=data,
    )


def test_every_event_type_has_a_field_allow_list() -> None:
    assert set(EVENT_DATA_FIELDS) == set(EventType)


def test_no_allowed_field_looks_like_personal_data_or_free_text() -> None:
    for event_type, fields in EVENT_DATA_FIELDS.items():
        assert not fields & PERSONAL_DATA_NAMES, f"{event_type}: {fields & PERSONAL_DATA_NAMES}"


@pytest.mark.parametrize("bad", sorted(PERSONAL_DATA_NAMES))
def test_add_event_refuses_personal_data_and_free_text_fields(bad: str) -> None:
    data = {**refund_data(uuid.uuid4()), bad: "something"}
    with pytest.raises(ValueError, match="not allowed"):
        add(EventType.REFUND_REQUESTED, data)


def test_add_event_refuses_unknown_event_types() -> None:
    with pytest.raises(ValueError, match="unknown event type"):
        add("refund.exploded", {})


def test_add_event_accepts_exactly_the_documented_fields_and_never_commits() -> None:
    session = FakeSession()
    row = add(
        EventType.REFUND_COMPLETED, {**refund_data(uuid.uuid4()), "provider_ref": "sim_1"}, session
    )
    assert session.added == [row]  # only added to the caller's session
    assert row.payload["data"]["provider_ref"] == "sim_1" and row.published_at is None
    assert row.payload["meta"] == {"request_id": None}


def test_data_values_are_json_safe() -> None:
    row = add(
        EventType.TICKET_CREATED,
        {"ticket_id": uuid.uuid4(), "user_id": uuid.uuid4(), "order_id": None, "priority": "high"},
    )
    assert all(isinstance(v, str | type(None)) for v in row.payload["data"].values())


def test_topics() -> None:
    assert topic_for("refund") == "ops.refunds" and topic_for("ticket", "x") == "x.tickets"
    assert (
        event_topics("p") == ["p.refunds", "p.tickets"]
        and dead_letter_topic("p") == "p.dead-letter"
    )


# --- the wire format ---


def test_a_valid_envelope_round_trips() -> None:
    raw = valid_envelope_bytes("refund.completed")
    env = parse_envelope(raw)
    assert env.version == ENVELOPE_VERSION and env.event_type == "refund.completed"
    assert parse_envelope(env.to_bytes()) == env
    assert set(env.model_dump()) == {
        "event_id",
        "event_type",
        "version",
        "occurred_at",
        "request_id",
        "aggregate_type",
        "aggregate_id",
        "data",
    }


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "empty_message"),
        (b"", "empty_message"),
        (b"{not json", "invalid_json"),
        (b"\xff\xfe\xfd", "invalid_json"),
        (b"42", "not_an_object"),
        (b'"a string"', "not_an_object"),
        (b"[]", "not_an_object"),
        (b"{}", "invalid_envelope"),
        (b'{"event_id": "x"}', "invalid_envelope"),
        (
            valid_envelope_bytes("refund.requested").replace(b'"event_id"', b'"event_idd"'),
            "invalid_envelope",
        ),
        (valid_envelope_bytes("refund.exploded"), "unknown_event_type"),
        (valid_envelope_bytes("refund.requested", version=2), "unsupported_version"),
        (valid_envelope_bytes("refund.requested", version=0), "unsupported_version"),
        (
            valid_envelope_bytes("refund.requested").replace(
                b'"data":{', b'"data":{"email":"a@b.c",'
            ),
            "unexpected_fields",
        ),
        (
            valid_envelope_bytes("ticket.created"),
            "unexpected_fields",
        ),  # refund fields on a ticket event
    ],
)
def test_unusable_messages_are_poison_with_a_reason(raw, reason) -> None:
    with pytest.raises(PoisonMessage) as exc:
        parse_envelope(raw)
    assert exc.value.reason == reason


def test_the_envelope_forbids_extra_top_level_fields() -> None:
    raw = valid_envelope_bytes().replace(b'"data"', b'"extra":1,"data"')
    with pytest.raises(PoisonMessage) as exc:
        parse_envelope(raw)
    assert exc.value.reason == "invalid_envelope"


def test_envelope_is_immutable() -> None:
    env = parse_envelope(valid_envelope_bytes())
    with pytest.raises(Exception):  # noqa: B017
        env.event_type = "x"  # type: ignore[misc]


# --- the documentation stays in step with the code ---


def test_docs_events_md_documents_every_event_type_and_field() -> None:
    text = DOC.read_text(encoding="utf-8")
    for event_type, fields in EVENT_DATA_FIELDS.items():
        assert f"`{event_type.value}`" in text, f"{event_type.value} is not documented"
        for f in fields:
            assert re.search(rf"`{re.escape(f)}`", text), (
                f"field {f} of {event_type.value} undocumented"
            )
    for envelope_field in EventEnvelope.model_fields:
        assert f"`{envelope_field}`" in text, f"envelope field {envelope_field} undocumented"
    for setting in ("outbox_max_attempts", "outbox_backoff_base_s", "outbox_backoff_max_s"):
        assert setting.upper() in text.upper()
