"""ClaudeLLMClient: the Anthropic Messages API behind the provider-neutral `LLMClient` interface.

* The API key is a `SecretStr` and goes only into the SDK client's constructor. It is never logged,
  never put in an exception message, never in a repr. Logs carry model, token usage, stop reason
  and timing only: never message content (it can hold personal data).
* Timeouts and ONE retry (`max_retries=1`, done by the SDK) for transient failures. After that
  `LLMUnavailable` is raised with a fixed, user-safe message. Failures a retry cannot fix (bad key,
  bad request) raise `LLMConfigError` immediately and are not retried.
* Every call sends `max_tokens`. Tools are sent only when there are any.
"""

import time
from collections.abc import Sequence
from typing import Any

import anthropic
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
    ToolUseBlock,
    Usage,
)

log = structlog.get_logger(__name__)

UNAVAILABLE = "The assistant is temporarily unavailable. Please try again in a moment."
MISCONFIGURED = "The assistant is not configured correctly. Please contact support."


def _wire_blocks(blocks: Sequence[Block]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            if b.text.strip():  # the API rejects empty text blocks
                out.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        else:
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.tool_use_id,
                    "content": b.content,
                    "is_error": b.is_error,
                }
            )
    return out


def to_wire_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Provider-neutral messages -> Anthropic. Tool results travel inside a USER message."""
    wire: list[dict[str, Any]] = []
    for m in messages:
        content = _wire_blocks(m.content)
        if content:
            wire.append(
                {"role": "assistant" if m.role == "assistant" else "user", "content": content}
            )
    return wire


def to_wire_tools(tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


class ClaudeLLMClient:
    name = "claude"

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str,
        timeout_s: float,
        max_retries: int = 1,
        http_client: Any = None,  # tests pass a client with a mock transport
    ) -> None:
        self._model = model
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            timeout=timeout_s,
            max_retries=max_retries,
            http_client=http_client,
        )

    def __repr__(self) -> str:  # never include anything secret
        return f"ClaudeLLMClient(model={self._model!r})"

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": to_wire_messages(messages),
        }
        if tools:
            kwargs["tools"] = to_wire_tools(tools)

        started = time.monotonic()
        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.APIConnectionError as exc:  # includes timeouts
            self._log_failure(exc, started)
            raise LLMUnavailable(UNAVAILABLE) from None
        except anthropic.APIStatusError as exc:
            self._log_failure(exc, started)
            if exc.status_code in (408, 409, 429) or exc.status_code >= 500:
                raise LLMUnavailable(UNAVAILABLE) from None
            raise LLMConfigError(MISCONFIGURED) from None
        except anthropic.AnthropicError as exc:
            self._log_failure(exc, started)
            raise LLMConfigError(MISCONFIGURED) from None

        content: list[Block] = []
        for b in resp.content:
            if b.type == "text":
                content.append(TextBlock(b.text))
            elif b.type == "tool_use":
                content.append(ToolUseBlock(b.id, b.name, dict(b.input)))  # other types ignored
        usage = Usage(resp.usage.input_tokens, resp.usage.output_tokens)
        log.info(
            "llm_call",
            llm="claude",
            model=self._model,
            stop_reason=resp.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return LLMResponse(tuple(content), resp.stop_reason or "end_turn", usage)

    def _log_failure(self, exc: Exception, started: float) -> None:
        # The class name and status only: the SDK's message can echo response bodies.
        log.warning(
            "llm_call_failed",
            llm="claude",
            model=self._model,
            error=type(exc).__name__,
            status_code=getattr(exc, "status_code", None),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
