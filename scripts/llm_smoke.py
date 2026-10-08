"""Two real calls to the configured Claude model, to check the key, the model name and tool use.

    backend/.venv/Scripts/python scripts/llm_smoke.py        # from the repo root

Needs ANTHROPIC_API_KEY in .env (this script never prints it, and nothing here is committed with
it). Costs a few hundred tokens. Without a key it calls nothing and exits 2. Exit codes: 0 ok,
1 the model call failed (the message says why), 2 no key.
"""

import asyncio
import sys
import time

from app.agent.llm.base import LLMError, Message, ToolDef
from app.agent.llm.claude import ClaudeLLMClient
from app.core.config import get_settings

DUMMY_TOOL = ToolDef(
    "list_orders",
    "List the customer's orders, newest first.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
    },
)


async def main() -> int:
    settings = get_settings()
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (put it in .env). Nothing was called.", file=sys.stderr)
        return 2
    client = ClaudeLLMClient(
        settings.anthropic_api_key,
        model=settings.anthropic_model,
        timeout_s=settings.llm_timeout_s,
    )
    print(f"model: {settings.anthropic_model}   timeout: {settings.llm_timeout_s:g}s")
    try:
        t0 = time.monotonic()
        out = await client.complete(
            system="You are a connectivity test. Answer in one short sentence.",
            messages=[Message.user("Reply with the single word: pong")],
            tools=[],
            max_tokens=20,
        )
        print(
            f"1. text call   ok in {time.monotonic() - t0:.1f}s: {out.text!r}  tokens={out.usage}"
        )

        t0 = time.monotonic()
        out = await client.complete(
            system="You help customers. Use the tools you are given.",
            messages=[Message.user("Please list my 3 most recent orders.")],
            tools=[DUMMY_TOOL],
            max_tokens=200,
        )
        proposed = [(u.name, u.input) for u in out.tool_uses]
        print(
            f"2. tool call   ok in {time.monotonic() - t0:.1f}s: stop_reason={out.stop_reason} "
            f"proposed={proposed or 'nothing (the model answered in text)'}"
        )
    except LLMError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
