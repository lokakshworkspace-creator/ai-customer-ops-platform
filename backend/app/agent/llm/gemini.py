"""GeminiLLMClient: the Google Gemini REST API (`generateContent`, v1beta) over plain httpx.

No SDK: one POST per model call. Documentation used (ai.google.dev, checked 2026-10-08):
request `{systemInstruction, contents[], tools[{functionDeclarations[]}], generationConfig}`;
a model turn has `role: "model"` and parts such as `{text}` or `{functionCall: {id?, name, args}}`;
the answer to a function call is a `{functionResponse: {id?, name, response}}` part in a `user`
turn; `thoughtSignature` is an opaque value on a part that must be sent back unchanged; the
response is `{candidates[{content, finishReason}], usageMetadata}`. The model name comes from
settings (LLM_MODEL): it is never hard-coded.

* The API key goes ONLY in the `x-goog-api-key` header, never in the URL (URLs end up in logs).
  It is a `SecretStr`; it is never logged, put in an exception message or shown in a repr.
  Logs carry model, token usage, finish reason and timing, never message content.
* A timeout, and ONE retry for transient failures (timeouts, connection errors, 408/429/5xx),
  then `LLMUnavailable` with a fixed user-safe message. A failure a retry cannot fix (bad key,
  bad request) raises `LLMConfigError` at once. Response bodies are never copied into errors.
* `max_tokens` is sent on every call as `generationConfig.maxOutputTokens`.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from typing import Any

import httpx
import structlog
from pydantic import SecretStr

from app.agent.llm.base import (
    Block,
    LLMConfigError,
    LLMResponse,
    LLMUnavailable,
    Message,
    TextBlock,
    ToolDef,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from app.agent.tools_view import gemini_declarations

log = structlog.get_logger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
UNAVAILABLE = "The assistant is temporarily unavailable. Please try again in a moment."
MISCONFIGURED = "The assistant is not configured correctly. Please contact support."
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
LOCAL_ID = "local-"  # prefix of ids we made up because the API sent none


def to_contents(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Provider-neutral messages -> Gemini `contents`. Roles: user->user, assistant->model, and
    tool results go back as `functionResponse` parts of a USER turn (matched to their call by
    id when the API gave one, and always by function name)."""
    names: dict[str, str] = {}  # tool_use id -> function name
    for m in messages:
        for b in m.content:
            if isinstance(b, ToolUseBlock):
                names[b.id] = b.name

    contents: list[dict[str, Any]] = []
    for m in messages:
        parts: list[dict[str, Any]] = []
        for b in m.content:
            if isinstance(b, TextBlock):
                if b.text.strip():  # empty text parts are rejected
                    parts.append({"text": b.text})
            elif isinstance(b, ToolUseBlock):
                call: dict[str, Any] = {"name": b.name, "args": b.input}
                if not b.id.startswith(LOCAL_ID):
                    call["id"] = b.id
                part: dict[str, Any] = {"functionCall": call}
                if b.signature:
                    part["thoughtSignature"] = b.signature  # sent back exactly as received
                parts.append(part)
            else:
                parts.append({"functionResponse": _function_response(b, names)})
        if parts:
            contents.append({"role": "model" if m.role == "assistant" else "user", "parts": parts})
    return contents


def _function_response(b: ToolResultBlock, names: dict[str, str]) -> dict[str, Any]:
    try:
        body = json.loads(b.content)
    except ValueError:
        body = None
    response = body if isinstance(body, dict) else {"output": b.content}
    out: dict[str, Any] = {"name": names.get(b.tool_use_id, "unknown_tool"), "response": response}
    if not b.tool_use_id.startswith(LOCAL_ID):
        out["id"] = b.tool_use_id
    return out


def from_candidate(candidate: dict[str, Any]) -> tuple[tuple[Block, ...], str]:
    blocks: list[Block] = []
    for part in (candidate.get("content") or {}).get("parts") or []:
        if part.get("thought"):
            continue  # a thought summary is not part of the answer
        if "functionCall" in part:
            fc = part["functionCall"]
            blocks.append(
                ToolUseBlock(
                    str(fc.get("id") or f"{LOCAL_ID}{uuid.uuid4().hex[:12]}"),
                    str(fc.get("name", "")),
                    dict(fc.get("args") or {}),
                    part.get("thoughtSignature"),
                )
            )
        elif isinstance(part.get("text"), str):
            blocks.append(TextBlock(part["text"]))
    finish = candidate.get("finishReason", "STOP")
    if finish == "MAX_TOKENS":
        stop = "max_tokens"
    elif any(isinstance(b, ToolUseBlock) for b in blocks):
        stop = "tool_use"
    else:
        stop = "end_turn"  # STOP, and also SAFETY / MALFORMED_FUNCTION_CALL etc.: no usable action
    return tuple(blocks), stop


class GeminiLLMClient:
    name = "gemini"

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str,
        timeout_s: float,
        retry_delay_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,  # tests pass a MockTransport
        base_url: str = BASE_URL,
    ) -> None:
        self._model = model
        self._retry_delay_s = retry_delay_s
        self._url = f"{base_url}/models/{model}:generateContent"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={"x-goog-api-key": api_key.get_secret_value()},
            transport=transport,
        )

    def __repr__(self) -> str:  # never anything secret
        return f"GeminiLLMClient(model={self._model!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": to_contents(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": gemini_declarations(tools)}]

        started = time.monotonic()
        data = await self._post(body, started)

        candidates = data.get("candidates") or []
        blocks, stop = from_candidate(candidates[0]) if candidates else ((), "end_turn")
        meta = data.get("usageMetadata") or {}
        usage = Usage(
            int(meta.get("promptTokenCount", 0)), int(meta.get("candidatesTokenCount", 0))
        )
        log.info(
            "llm_call",
            llm="gemini",
            model=self._model,
            stop_reason=stop,
            blocked=not candidates,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return LLMResponse(blocks, stop, usage)

    async def _post(self, body: dict[str, Any], started: float) -> dict[str, Any]:
        for attempt in (1, 2):  # the call, and exactly one retry
            try:
                resp = await self._client.post(self._url, json=body)
            except httpx.TransportError as exc:  # timeouts and connection failures
                self._log_failure(type(exc).__name__, None, started)
                if attempt == 2:
                    raise LLMUnavailable(UNAVAILABLE) from None
                await asyncio.sleep(self._retry_delay_s)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    self._log_failure("InvalidJSON", 200, started)
                    raise LLMUnavailable(UNAVAILABLE) from None
            self._log_failure("HTTPStatusError", resp.status_code, started)
            if resp.status_code in TRANSIENT_STATUS:
                if attempt == 2:
                    raise LLMUnavailable(UNAVAILABLE)
                await asyncio.sleep(self._delay(resp))
                continue
            raise LLMConfigError(MISCONFIGURED)  # 400 / 401 / 403 / 404: retrying cannot help
        raise LLMUnavailable(UNAVAILABLE)  # pragma: no cover - the loop always returns or raises

    def _delay(self, resp: httpx.Response) -> float:
        try:
            wanted = float(resp.headers.get("retry-after", ""))
        except ValueError:
            return self._retry_delay_s
        return min(max(wanted, 0.0), 5.0)  # never wait long inside a request

    def _log_failure(self, error: str, status: int | None, started: float) -> None:
        # The class name and status only: an error body can echo what we sent.
        log.warning(
            "llm_call_failed",
            llm="gemini",
            model=self._model,
            error=error,
            status_code=status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
