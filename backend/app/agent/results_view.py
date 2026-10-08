"""What the tool executor's outcome looks like to (1) the model and (2) the UI.

Both are built by the BACKEND from the structured `ToolOutcome`; neither is model-authored text.

* `model_view`: an ALLOW-LIST of fields, size-capped (`MAX_RESULT_BYTES`). It is untrusted DATA
  for the model (order item names can hold anything a merchant typed). It never contains emails,
  names, user ids, notes or password hashes, and item names are truncated.
* `ui_summary`: one short, human line plus the machine fields. The UI shows this next to the
  model's reply, so what the backend decided is visible even if the model words it badly.
"""

import json
from typing import Any

from app.db.models.enums import ToolCallStatus
from app.tools.executor import ToolOutcome

MAX_RESULT_BYTES = 2048  # per tool result sent to the model
MAX_ITEM_NAME = 80
GENERIC_ERRORS = {
    "INTERNAL_ERROR": "The action could not be completed because of an internal error."
}
ERROR_STATUSES = frozenset({ToolCallStatus.invalid, ToolCallStatus.failed})


def _order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "status": order.get("status"),
        "total_amount": order.get("total_amount"),
        "currency": order.get("currency"),
        "placed_at": order.get("placed_at"),
        "items": [
            {
                "name": str(i.get("name", ""))[:MAX_ITEM_NAME],
                "quantity": i.get("quantity"),
                "unit_price": i.get("unit_price"),
            }
            for i in order.get("items", [])
        ],
    }


def _refund(refund: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "order_id", "amount", "currency", "status", "reason_code")
    return {k: refund.get(k) for k in keys}


def allowed_data(tool: str, data: dict[str, Any] | None) -> dict[str, Any]:
    """The fields of a tool's result the model may see."""
    data = data or {}
    if tool == "list_orders":
        return {"orders": [_order(o) for o in data.get("orders", [])], "total": data.get("total")}
    if tool == "get_order":
        return {"order": _order(data["order"])} if "order" in data else {}
    if tool == "cancel_order":
        return {"order_id": data.get("order_id"), "status": data.get("status")}
    if tool == "create_ticket":
        t = data.get("ticket") or {}
        return {"ticket": {k: t.get(k) for k in ("id", "subject", "status", "priority")}}
    if tool == "request_refund":
        out: dict[str, Any] = {"refund": _refund(data.get("refund") or {})}
        out["approval_id"] = data.get("approval_id")
        out["replayed"] = bool(data.get("replayed"))
        return out
    return {}  # a tool with no entry shows the model nothing but its status


def _cap(view: dict[str, Any]) -> dict[str, Any]:
    """Shrink until the JSON fits: drop trailing orders first, then give up on the data."""

    def size() -> int:
        return len(json.dumps(view, separators=(",", ":")).encode("utf-8"))

    data = view.get("data", {})
    orders = data.get("orders")
    while size() > MAX_RESULT_BYTES and orders:
        orders.pop()
        data["truncated"] = True
    if size() > MAX_RESULT_BYTES:
        view["data"] = {"truncated": True}
    return view


def model_view(outcome: ToolOutcome) -> dict[str, Any]:
    view: dict[str, Any] = {"tool": outcome.tool_name, "status": outcome.status.value}
    if outcome.decision is not None:
        view["decision"] = outcome.decision.value
        view["reason_code"] = outcome.reason_code
        view["reason"] = outcome.reason
    if outcome.error_code:
        view["error_code"] = outcome.error_code
        view["error"] = GENERIC_ERRORS.get(outcome.error_code, outcome.error)
    if outcome.details:
        view["problems"] = [f"{d['field']}: {d['message']}" for d in outcome.details[:5]]
    view["data"] = allowed_data(outcome.tool_name, outcome.data)
    return _cap(view)


def tool_result_text(outcome: ToolOutcome) -> str:
    return json.dumps(model_view(outcome), separators=(",", ":"), default=str)


def is_error(outcome: ToolOutcome) -> bool:
    return outcome.status in ERROR_STATUSES


def awaiting_approval(outcome: ToolOutcome) -> bool:
    refund = (outcome.data or {}).get("refund") or {}
    return outcome.status == ToolCallStatus.awaiting_approval or (
        refund.get("status") == "pending_approval"
    )


def _summary_text(outcome: ToolOutcome, data: dict[str, Any]) -> str:
    if outcome.tool_name == "request_refund" and data.get("refund", {}).get("status"):
        r = data["refund"]
        return f"Refund of {r['amount']} {r['currency']}: {r['status']}"
    if outcome.status == ToolCallStatus.denied:
        return outcome.reason or "Not allowed"
    if outcome.status in ERROR_STATUSES:
        return GENERIC_ERRORS.get(outcome.error_code or "", outcome.error or "Failed")
    if outcome.tool_name == "list_orders":
        return f"Found {data.get('total', 0)} order(s)"
    if outcome.tool_name == "get_order":
        return f"Order {data.get('order', {}).get('status', '')}".strip()
    if outcome.tool_name == "cancel_order":
        return f"Order {data.get('status', '')}".strip()
    if outcome.tool_name == "create_ticket":
        return "Support ticket opened"
    return outcome.status.value


def ui_summary(outcome: ToolOutcome) -> dict[str, Any]:
    data = allowed_data(outcome.tool_name, outcome.data)
    return {
        "tool": outcome.tool_name,
        "status": outcome.status.value,
        "decision": outcome.decision.value if outcome.decision else None,
        "reason_code": outcome.reason_code or outcome.error_code,
        "summary": _summary_text(outcome, data),
        "tool_call_id": str(outcome.tool_call_id),
    }
