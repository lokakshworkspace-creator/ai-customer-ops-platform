"""The tools as the LLM sees them: ONLY `agent_visible` tools, with model-friendly JSON schemas.

Why the raw Pydantic schema is not handed over as it is:

* money fields are `anyOf [number, string]` plus non-standard keys (`decimal_places`, `gt`, `le`).
  A model told "a number is fine" sends `250.0`, which our validator (correctly) rejects, so money
  is presented as a decimal STRING only;
* `Optional[X]` becomes `anyOf [X, null]`, `$defs`/`$ref` and `title` clutter everything;
* `reason_code` is a free string in the schema, but the valid reasons are configuration
  (`rules.yaml`), so the model is given them as an enum. It is NOT told which reasons or amounts
  are auto-approved: policy decides that, not the prompt.

The view is derived from the registry, so adding a tool or changing a schema changes what the model
sees. Identity (`user_id`, `role`) and idempotency keys are not fields of any tool: the executor
rejects them (`extra="forbid"`) and the backend derives the key itself.
"""

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from app.agent.llm.base import ToolDef
from app.policy.rules import Rules
from app.tools.registry import ToolRegistry

_NON_STANDARD = {"title", "decimal_places", "max_digits", "gt", "ge", "lt", "le"}
MONEY_FIELDS = frozenset({"amount"})
MONEY_PATTERN = r"^[0-9]{1,10}(\.[0-9]{1,2})?$"
MONEY_HINT = ' A decimal string such as "250.00", never a JSON number.'


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    """Inline `$ref`s (keeping sibling keys such as description/default)."""
    if isinstance(node, list):
        return [_resolve(x, defs) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: _resolve(v, defs) for k, v in node.items() if k != "$ref"}
    if "$ref" in node:
        target = _resolve(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        out = {**target, **out}
    return out


def _clean(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":  # keys here are FIELD names: never drop one called "title"
            out[key] = {name: _clean(sub) for name, sub in value.items()}
        elif key not in _NON_STANDARD:
            out[key] = _clean(value)
    if out.get("default", 0) is None:
        out.pop("default")
    if "anyOf" in out:
        options = out.pop("anyOf")
        options = [o for o in options if o.get("type") != "null"]
        if any(o.get("type") == "number" for o in options):
            options = [o for o in options if o.get("type") != "number"]  # money: string only
        out = {**out, **options[0]} if len(options) == 1 else {**out, "anyOf": options}
    return out


def to_llm_schema(model: type[BaseModel], rules: Rules) -> dict[str, Any]:
    raw = model.model_json_schema()
    schema = _clean(_resolve({k: v for k, v in raw.items() if k != "$defs"}, raw.get("$defs", {})))
    schema.pop("description", None)  # the class docstring; the tool's own description is used
    props: dict[str, Any] = schema.get("properties", {})
    for name in MONEY_FIELDS & props.keys():
        note = props[name].get("description", "")
        props[name] = {
            "type": "string",
            "pattern": MONEY_PATTERN,
            "description": (note + MONEY_HINT).strip(),
        }
    if "reason_code" in props:
        props["reason_code"] = {
            "type": "string",
            "enum": sorted(rules.refund.valid_reasons),
            "description": props["reason_code"].get("description", ""),
        }
    return schema


def agent_tool_defs(registry: ToolRegistry, rules: Rules) -> list[ToolDef]:
    """What the model may be shown and may propose. Everything else does not exist for it."""
    return [
        ToolDef(t.name, t.description, to_llm_schema(t.input_model, rules))
        for t in registry.agent_tools()
    ]


# --- Gemini --------------------------------------------------------------------------------------
# Gemini's function parameters are NOT arbitrary JSON Schema: they are an OpenAPI-style `Schema`
# object (https://ai.google.dev/api/caching#Schema) with upper-case types and a fixed set of keys.
# `additionalProperties`, `$ref`, `title`, `default` and non-standard `format`s are not part of it.

GEMINI_TYPES = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}
GEMINI_KEYS = frozenset(
    {
        "type", "format", "description", "nullable", "enum", "items", "properties", "required",
        "minimum", "maximum", "minItems", "maxItems", "pattern", "minLength", "maxLength",
        "anyOf", "propertyOrdering",
    }
)  # fmt: skip
GEMINI_FORMATS = frozenset({"enum", "date-time", "int32", "int64", "float", "double"})


def to_gemini_schema(node: Any) -> Any:
    """Our cleaned schema (to_llm_schema) -> the subset Gemini accepts."""
    if isinstance(node, list):
        return [to_gemini_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in GEMINI_KEYS:
            continue  # additionalProperties, default, title, ...
        if key == "type":
            out[key] = GEMINI_TYPES.get(value, value)
        elif key == "properties":
            out[key] = {name: to_gemini_schema(sub) for name, sub in value.items()}
        elif key == "format" and value not in GEMINI_FORMATS:
            continue  # e.g. "uuid": unsupported, so only a hint in the description is kept
        else:
            out[key] = to_gemini_schema(value)
    if node.get("format") == "uuid" and "UUID" not in out.get("description", ""):
        out["description"] = (out.get("description", "") + " Format: UUID.").strip()
    if "properties" in out:
        out["propertyOrdering"] = list(out["properties"])
    return out


def gemini_declarations(defs: Sequence[ToolDef]) -> list[dict[str, Any]]:
    """The `functionDeclarations` array of a Gemini request, one entry per tool."""
    return [
        {
            "name": d.name,
            "description": d.description,
            "parameters": to_gemini_schema(d.input_schema),
        }
        for d in defs
    ]
