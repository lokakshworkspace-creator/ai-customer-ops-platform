"""What the LLM is shown: only agent_visible tools, with schemas it can actually satisfy."""

import json
import os
import uuid
from pathlib import Path

from app.agent.tools_view import (
    GEMINI_FORMATS,
    GEMINI_KEYS,
    GEMINI_TYPES,
    MONEY_PATTERN,
    agent_tool_defs,
    gemini_declarations,
)
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
        "search_knowledge_base",
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


# --- Gemini: the restricted schema subset, with a golden snapshot per tool ----------------------

GOLDEN = Path(__file__).parent / "golden" / "gemini_tools.json"


def gemini_by_tool() -> dict:
    return {d["name"]: d for d in gemini_declarations(list(DEFS.values()))}


def test_gemini_golden_snapshot_per_tool() -> None:
    """The exact function declarations sent to Gemini. A change here is a change to what the model
    is told, so it must be deliberate: regenerate with UPDATE_GOLDEN=1 and review the diff."""
    actual = gemini_by_tool()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(actual) == set(golden) == set(DEFS)
    for name in sorted(actual):
        assert actual[name] == golden[name], f"{name}: Gemini declaration changed"


def check_gemini_schema(node: dict, where: str) -> None:
    """Every schema node uses only keys from Gemini's documented Schema object."""
    assert set(node) <= GEMINI_KEYS, (where, set(node) - GEMINI_KEYS)
    if "type" in node:
        assert node["type"] in GEMINI_TYPES.values(), (where, node["type"])
    assert node.get("format") in (None, *GEMINI_FORMATS), where  # uuid is not a Gemini format
    for name, sub in node.get("properties", {}).items():
        check_gemini_schema(sub, f"{where}.{name}")
    if "items" in node:
        check_gemini_schema(node["items"], f"{where}[]")
    for i, sub in enumerate(node.get("anyOf", [])):
        check_gemini_schema(sub, f"{where}|{i}")


def test_gemini_schemas_use_only_the_documented_subset() -> None:
    for name, decl in gemini_by_tool().items():
        check_gemini_schema(decl["parameters"], name)
        assert set(decl) == {"name", "description", "parameters"}


def test_gemini_keeps_what_matters_and_flags_uuids_in_the_description() -> None:
    refund = gemini_by_tool()["request_refund"]["parameters"]
    assert refund["type"] == "OBJECT" and refund["required"] == [
        "order_id",
        "amount",
        "reason_code",
    ]
    assert refund["propertyOrdering"] == ["order_id", "amount", "reason_code", "note"]
    assert refund["properties"]["amount"]["type"] == "STRING"
    assert refund["properties"]["amount"]["pattern"] == MONEY_PATTERN
    assert refund["properties"]["reason_code"]["enum"] == sorted(RULES.refund.valid_reasons)
    assert "format" not in refund["properties"]["order_id"]
    assert "UUID" in refund["properties"]["order_id"]["description"]  # the hint survives
    limit = gemini_by_tool()["list_orders"]["parameters"]["properties"]["limit"]
    assert limit["type"] == "INTEGER" and (limit["minimum"], limit["maximum"]) == (1, 100)


def test_gemini_declarations_never_expose_admin_tools_or_identity_fields() -> None:
    text = json.dumps(gemini_by_tool())
    for forbidden in ("approve_refund", "reject_refund", "approval_id", "user_id", "idempotency"):
        assert forbidden not in text
