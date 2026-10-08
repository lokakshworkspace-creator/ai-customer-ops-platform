"""The LLM interface. Nothing in the application talks to a model except through `LLMClient`.

Golden rule 1: the model PROPOSES. A response can contain `ToolUseBlock`s; they are proposals that
the orchestrator hands to the tool executor (validate -> authorize -> policy -> execute -> log).
Nothing here executes anything.

Conventions the rest of the agent relies on:

* Messages are provider-neutral. Role `tool` carries `ToolResultBlock`s (a client maps it to
  whatever its API wants; Anthropic wants them inside a user message).
* The content of a `ToolResultBlock` is JSON text produced by the BACKEND (agent/results_view.py):
  ``{"tool", "status", "decision", "reason_code", "reason", "data"}``. It is untrusted DATA for the
  model, never instructions, and it never contains emails, names or password hashes.
* `max_tokens` is a required argument of every call (it bounds cost and latency).
* Every client has a `name` ("fake" or "claude"); the API reports it so a person can tell which
  kind of model produced a reply.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class LLMError(Exception):
    """Base class. Messages are safe to show to a user and never contain secrets."""


class LLMUnavailable(LLMError):
    """The model could not be reached or kept failing (timeout, 5xx, rate limit) after a retry."""


class LLMConfigError(LLMError):
    """The model rejected the request in a way retrying will not fix (bad key, bad request)."""


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """A PROPOSAL by the model. `id` is chosen by the model/provider and only used to pair the
    result with the proposal; it is never used for anything that matters (not for idempotency)."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock
Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: tuple[Block, ...]

    @staticmethod
    def user(text: str) -> "Message":
        return Message("user", (TextBlock(text),))

    @staticmethod
    def assistant(*blocks: Block) -> "Message":
        return Message("assistant", tuple(blocks))

    @staticmethod
    def tool(*results: ToolResultBlock) -> "Message":
        return Message("tool", tuple(results))


@dataclass(frozen=True, slots=True)
class ToolDef:
    """A tool as the model sees it (agent/tools_view.py builds these from the registry)."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: tuple[Block, ...]  # TextBlock and ToolUseBlock only
    stop_reason: str = "end_turn"  # "end_turn" | "tool_use" | "max_tokens"
    usage: Usage = field(default_factory=Usage)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock)).strip()

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]


class LLMClient(Protocol):
    name: Literal["fake", "claude"]

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> LLMResponse: ...
