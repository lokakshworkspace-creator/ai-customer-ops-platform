"""The wire format of an event (see docs/events.md).

{
  "event_id":       "<uuid>",          # the outbox row id: stable across redeliveries
  "event_type":     "refund.completed",
  "version":        1,                 # envelope/schema version
  "occurred_at":    "<ISO-8601 UTC>",  # when the outbox row was written (transaction start)
  "request_id":     "<string>|null",   # the HTTP request that caused it
  "aggregate_type": "refund",
  "aggregate_id":   "<uuid>",          # also the Kafka message key
  "data":           { ...ids and state only... }
}
"""

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.events.types import ENVELOPE_VERSION, EVENT_DATA_FIELDS, EventType


class PoisonMessage(Exception):
    """A message that can never be processed (so retrying it is pointless): it is dead-lettered."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: uuid.UUID
    event_type: str
    version: int = ENVELOPE_VERSION
    occurred_at: datetime
    request_id: str | None = None
    aggregate_type: str
    aggregate_id: str
    data: dict[str, Any]

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode("utf-8")


def envelope_from_row(row: Any) -> EventEnvelope:
    """Build the envelope from an outbox row (mapping-like: id, event_type, payload, ...)."""
    payload = row["payload"] or {}
    return EventEnvelope(
        event_id=row["id"],
        event_type=row["event_type"],
        occurred_at=row["created_at"],
        request_id=(payload.get("meta") or {}).get("request_id"),
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        data=payload.get("data") or {},
    )


def parse_envelope(raw: bytes | None) -> EventEnvelope:
    """Strictly parse and validate a message. Raises PoisonMessage(reason) if it is unusable."""
    if not raw:
        raise PoisonMessage("empty_message")
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PoisonMessage("invalid_json", type(exc).__name__) from None
    if not isinstance(obj, dict):
        raise PoisonMessage("not_an_object", type(obj).__name__)
    try:
        env = EventEnvelope.model_validate(obj)
    except ValidationError as exc:
        fields = ",".join(sorted({str(e["loc"][0]) if e["loc"] else "?" for e in exc.errors()}))
        raise PoisonMessage("invalid_envelope", fields) from None
    if env.version != ENVELOPE_VERSION:
        raise PoisonMessage("unsupported_version", str(env.version))
    try:
        event_type = EventType(env.event_type)
    except ValueError:
        raise PoisonMessage("unknown_event_type", env.event_type[:100]) from None
    unexpected = set(env.data) - EVENT_DATA_FIELDS[event_type]
    if unexpected:
        raise PoisonMessage("unexpected_fields", ",".join(sorted(unexpected)))
    return env
