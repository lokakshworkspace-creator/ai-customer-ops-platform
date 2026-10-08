"""Fake LLMs: used by ALL tests, and by the app when no ANTHROPIC_API_KEY is set (dev only).

* `ScriptedLLM`: plays back an exact script. Tests use it to put precise proposals in the model's
  mouth and to inspect what the model was SHOWN (`.calls`: the system prompt, the messages, the
  tools and `max_tokens` of every call).
* `ObedientFakeLLM`: a deliberately GULLIBLE rule-based model. It follows instructions it finds in
  the user's text or in tool results (including "call approve_refund", `user_id=...`,
  `idempotency_key=...`), even for tools it was not offered. That is the point: it is the worst
  case a real model could be talked into, so every backend defence (agent gate, schemas,
  authorization, policy, ownership) gets exercised by it. It is the dev default, the demo's model
  and the injection tests' model. Neither fake ever calls a network.
"""

import itertools
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agent.llm.base import (
    LLMResponse,
    Message,
    TextBlock,
    ToolDef,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

_ids = itertools.count(1)


def say(text: str) -> LLMResponse:
    return LLMResponse((TextBlock(text),), "end_turn", Usage(10, max(1, len(text) // 4)))


def call(name: str, /, **input: Any) -> LLMResponse:
    return calls((name, input))


def calls(*proposals: tuple[str, dict[str, Any]]) -> LLMResponse:
    blocks = tuple(ToolUseBlock(f"toolu_fake_{next(_ids)}", n, dict(a)) for n, a in proposals)
    return LLMResponse(blocks, "tool_use", Usage(10, 20))


@dataclass(frozen=True, slots=True)
class LLMCall:
    """What the model was shown on one call."""

    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolDef, ...]
    max_tokens: int


Step = LLMResponse | Exception | Callable[[LLMCall], LLMResponse]


class _Recording:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []
        self.on_call: Callable[[LLMCall], None] | None = None  # test hook, runs before answering

    def _record(self, system: str, messages: Sequence[Message], tools: Sequence[ToolDef], mt: int):
        call_ = LLMCall(system, tuple(messages), tuple(tools), mt)
        self.calls.append(call_)
        if self.on_call is not None:
            self.on_call(call_)
        return call_


class ScriptedLLM(_Recording):
    def __init__(self, *steps: Step) -> None:
        super().__init__()
        self._steps = list(steps)

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> LLMResponse:
        seen = self._record(system, messages, tools, max_tokens)
        if not self._steps:
            raise AssertionError(f"ScriptedLLM has no step left for call #{len(self.calls)}")
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(seen) if callable(step) else step


# --- the gullible one ---------------------------------------------------------------------------

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_RE = re.compile(_UUID)
_CALL_RE = re.compile(r"\bcall\s+([a-z]+(?:_[a-z]+)+)", re.I)
_REFUND_RE = re.compile(
    r"\brefund\b[^0-9\n]{0,24}?(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I
)
_EXTRA_RE = re.compile(r"\b(user_id|role|idempotency_key)\s*=\s*([^\s,;]+)", re.I)
_ZERO = "00000000-0000-0000-0000-000000000000"


def _money(raw: str) -> str:
    try:
        return format(Decimal(raw.replace(",", "")).quantize(Decimal("0.01")), "f")
    except InvalidOperation:  # pragma: no cover - the regex only admits numbers
        return "1.00"


class ObedientFakeLLM(_Recording):
    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> LLMResponse:
        self._record(system, messages, tools, max_tokens)
        user_text, results = self._this_turn(messages)
        return self._decide(user_text, results)

    # -- reading the conversation ---------------------------------------------------------------

    @staticmethod
    def _this_turn(messages: Sequence[Message]) -> tuple[str, list[tuple[str, Any, str]]]:
        """The latest user text and the (tool name, parsed JSON, raw text) results after it."""
        start = max(
            (
                i
                for i, m in enumerate(messages)
                if m.role == "user" and any(isinstance(b, TextBlock) for b in m.content)
            ),
            default=0,
        )
        user_text = " ".join(
            b.text
            for b in (messages[start].content if messages else ())
            if isinstance(b, TextBlock)
        )
        names: dict[str, str] = {}
        results: list[tuple[str, Any, str]] = []
        for m in messages[start + 1 :]:
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    names[b.id] = b.name
                elif isinstance(b, ToolResultBlock):
                    try:
                        parsed = json.loads(b.content)
                    except ValueError:
                        parsed = None
                    results.append((names.get(b.tool_use_id, "?"), parsed, b.content))
        return user_text, results

    # -- deciding -------------------------------------------------------------------------------

    def _decide(self, user_text: str, results: list[tuple[str, Any, str]]) -> LLMResponse:
        executed = {name for name, _, _ in results}
        sources = "\n".join([user_text, *(raw for _, _, raw in results)])
        extras = {k.lower(): v for k, v in _EXTRA_RE.findall(sources)}

        # 1. Gullibility: any "call <tool_name>" in the user's text OR in a tool result is obeyed,
        #    for any tool name, offered or not, with whatever identity fields it was told to add.
        told = [(m.group(1).lower(), m) for m in _CALL_RE.finditer(sources)]
        for tool, m in told:
            if tool not in executed:
                # the id right after "call <tool>" is the one it was told to use
                near = _UUID_RE.match(sources[m.end() :].lstrip())
                args = self._args_for(tool, sources, near.group(0) if near else None)
                return call(tool, **{**args, **extras})
        if any(tool in executed for tool, _ in told):  # it did what it was told; now it reports
            return say(self._summary(results))

        lowered = user_text.lower()
        # 2. A refund request: "refund me", "refund 250", "... and refund 100000".
        if "refund" in lowered:
            if "request_refund" in executed:
                return say(self._summary(results))
            if "list_orders" not in executed:
                return call("list_orders")
            order = self._pick_order(user_text, results)
            if order is None:
                return say("I could not find an order to refund.")
            m = _REFUND_RE.search(user_text)
            amount = _money(m.group(1)) if m else _money(str(order.get("total_amount", "1")))
            reason = "damaged" if "damag" in lowered else "other"
            return call(
                "request_refund",
                order_id=order["id"],
                amount=amount,
                reason_code=reason,
                note="Requested in chat",
                **extras,
            )

        # 3. Order questions.
        if "order" in lowered:
            if "list_orders" not in executed:
                return call("list_orders", **extras)
            return say(self._summary(results))

        if results:
            return say(self._summary(results))
        return say("Hello! I can look up your orders, request refunds and open support tickets.")

    @staticmethod
    def _args_for(tool: str, sources: str, near: str | None) -> dict[str, Any]:
        found = _UUID_RE.findall(sources)
        first = near or (found[0] if found else _ZERO)
        if tool in {"approve_refund", "reject_refund"}:
            return {"approval_id": first}
        if tool in {"get_order", "cancel_order"}:
            return {"order_id": first}
        if tool == "request_refund":
            return {"order_id": first, "amount": "1.00", "reason_code": "other"}
        if tool == "create_ticket":
            return {"subject": "Requested via chat", "body": sources[:200]}
        return {}

    @staticmethod
    def _pick_order(user_text: str, results: list[tuple[str, Any, str]]) -> dict[str, Any] | None:
        orders: list[dict[str, Any]] = []
        for name, parsed, _ in results:
            if name == "list_orders" and isinstance(parsed, dict):
                orders = ((parsed.get("data") or {}).get("orders")) or []
        if not orders:
            return None
        named = {u.lower() for u in _UUID_RE.findall(user_text)}
        for o in orders:
            if str(o.get("id", "")).lower() in named:
                return o
        for o in orders:
            if o.get("status") in {"delivered", "paid"}:
                return o
        return orders[0]

    @staticmethod
    def _summary(results: list[tuple[str, Any, str]]) -> str:
        if not results:
            return "Done."
        parts = []
        for name, parsed, _ in results:
            if not isinstance(parsed, dict):
                parts.append(f"{name}: no result")
                continue
            status = parsed.get("status", "?")
            detail = parsed.get("reason_code") or ""
            refund = (parsed.get("data") or {}).get("refund") or {}
            if refund.get("status"):
                status = f"{status}, refund {refund['status']}"
            parts.append(f"{name}: {status}" + (f" ({detail})" if detail else ""))
        return "Here is what happened. " + "; ".join(parts) + "."
