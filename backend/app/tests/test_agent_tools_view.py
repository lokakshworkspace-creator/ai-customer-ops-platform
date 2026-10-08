"""What the LLM is shown: only agent_visible tools, with schemas it can actually satisfy."""

import json
import uuid

from app.agent.tools_view import MONEY_PATTERN, agent_tool_defs
from app.policy.rules import get_rules
from app.tests.policy_helpers import make_rules
from app.tools.default import build_default_registry
from app.tools.schemas import CreateTicketInput, ListOrdersInput, RequestRefundInput

RULES = make_rules()
REGISTRY = build_default_registry(RULES)
DEFS = {d.name: d for d in agent_tool_defs(REGISTRY, RULES)}
FORBIDDEN_FIELDS = {
    "user_id",
    "actor_id",
    "role",
    "idempotency_key",
    "request_hash",
    "tool_call_id",
}
NON_STANDARD = {"title", "decimal_places", "gt", "le", "$defs", "$ref"}


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def test_the_model_sees_exactly_the_agent_visible_tools() -> None:
    assert set(DEFS) == {
        "get_order",
        "list_orders",
        "create_ticket",
        "cancel_order",
        "request_refund",
    }
    assert set(DEFS) == {t.name for t in REGISTRY.all() if t.agent_visible}


def test_admin_tools_never_reach_the_model() -> None:
    assert "approve_refund" not in DEFS and "reject_refund" not in DEFS
    everything = json.dumps({n: [d.description, d.input_schema] for n, d in DEFS.items()})
    assert "approve_refund" not in everything and "reject_refund" not in everything
    assert "approval_id" not in everything  # the admin tools' argument does not exist for the model


def test_tool_descriptions_come_from_the_registry() -> None:
    for name, d in DEFS.items():
        assert d.description == REGISTRY.get(name).description


def test_no_tool_has_a_field_for_identity_or_an_idempotency_key() -> None:
    for d in DEFS.values():
        props = d.input_schema.get("properties", {})
        assert not FORBIDDEN_FIELDS & set(props), d.name
        assert d.input_schema["additionalProperties"] is False  # nothing else is accepted either


def test_schemas_are_plain_json_schema() -> None:
    for d in DEFS.values():
        assert d.input_schema["type"] == "object"
        for node in walk(d.input_schema):
            assert not NON_STANDARD & set(node), (d.name, NON_STANDARD & set(node))
            assert "anyOf" not in node, d.name  # optionals are just "not required"
        json.dumps(d.input_schema)  # serialisable


def test_money_is_a_decimal_string_never_a_number() -> None:
    amount = DEFS["request_refund"].input_schema["properties"]["amount"]
    assert amount["type"] == "string" and amount["pattern"] == MONEY_PATTERN
    assert "never a JSON number" in amount["description"]
    import re

    assert all(re.fullmatch(MONEY_PATTERN, ok) for ok in ("250", "250.00", "0.5", "9999999999.99"))
    assert not any(
        re.fullmatch(MONEY_PATTERN, bad) for bad in ("-1", "1.234", "1e3", "250.0.0", "")
    )


def test_reasons_come_from_rules_yaml_and_auto_approval_is_not_revealed() -> None:
    schema = DEFS["request_refund"].input_schema
    reason = schema["properties"]["reason_code"]
    assert reason["enum"] == sorted(get_rules().refund.valid_reasons)
    text = json.dumps(schema) + DEFS["request_refund"].description
    assert "500.00" not in text and "auto" not in text.lower()  # the model is not told the limits
    assert schema["required"] == ["order_id", "amount", "reason_code"]


def test_a_custom_reason_list_changes_what_the_model_sees() -> None:
    rules = make_rules(
        **{"refund.valid_reasons": ["damaged", "other"], "refund.auto_approve_reasons": ["damaged"]}
    )
    defs = {d.name: d for d in agent_tool_defs(build_default_registry(rules), rules)}
    assert defs["request_refund"].input_schema["properties"]["reason_code"]["enum"] == [
        "damaged",
        "other",
    ]


def test_optional_fields_and_enums_are_flattened() -> None:
    ticket = DEFS["create_ticket"].input_schema
    assert ticket["required"] == ["subject", "body"]
    assert ticket["properties"]["order_id"]["format"] == "uuid"
    assert ticket["properties"]["priority"]["enum"] == ["low", "normal", "high"]
    assert ticket["properties"]["priority"]["description"]
    assert DEFS["list_orders"].input_schema["properties"]["limit"]["maximum"] == 100


def test_every_field_has_a_description() -> None:
    for d in DEFS.values():
        for field, prop in d.input_schema["properties"].items():
            assert prop.get("description"), (d.name, field)


def test_what_the_schema_promises_the_validator_accepts() -> None:
    RequestRefundInput.model_validate(
        {"order_id": str(uuid.uuid4()), "amount": "250.00", "reason_code": "damaged"}
    )
    CreateTicketInput.model_validate({"subject": "s", "body": "b", "priority": "high"})
    ListOrdersInput.model_validate({"limit": 100})
