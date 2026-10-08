"""Two real calls to the Gemini API, to check your key, the model name and function calling.

    backend/.venv/Scripts/python scripts/llm_smoke.py        # from the repo root

Needs GEMINI_API_KEY and LLM_MODEL in .env (a free Google AI Studio key is enough; LLM_MODEL is a
Gemini model name such as one of the "flash-lite" models listed at
https://ai.google.dev/gemini-api/docs/models). This script never prints the key. It costs a few
hundred tokens and is the ONLY thing in the repository that talks to the real service: the unit
tests use a mock transport. Without a key or a model it calls nothing and exits 2. Exit codes:
0 ok, 1 the call failed (the message says why), 2 not configured.
"""

import asyncio
import sys
import time

from app.agent.llm.base import LLMError, Message, ToolDef
from app.agent.llm.gemini import GeminiLLMClient
from app.core.config import get_settings

DUMMY_TOOL = ToolDef(
    "list_orders",
    "List the customer's orders, newest first.",
    {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many orders.",
                "minimum": 1,
                "maximum": 100,
            }
        },
    },
)


async def main() -> int:
    settings = get_settings()
    if settings.gemini_api_key is None or not settings.llm_model:
        print(
            "GEMINI_API_KEY and LLM_MODEL must both be set in .env. Nothing was called.",
            file=sys.stderr,
        )
        return 2
    client = GeminiLLMClient(
        settings.gemini_api_key, model=settings.llm_model, timeout_s=settings.llm_timeout_s
    )
    print(f"model: {settings.llm_model}   timeout: {settings.llm_timeout_s:g}s")
    try:
        t0 = time.monotonic()
        out = await client.complete(
            system="You are a connectivity test. Answer in one short sentence.",
            messages=[Message.user("Reply with the single word: pong")],
            tools=[],
            max_tokens=64,
        )
        print(
            f"1. text call   ok in {time.monotonic() - t0:.1f}s: {out.text!r}  tokens={out.usage}"
        )

        t0 = time.monotonic()
        out = await client.complete(
            system="You help customers. Use the tools you are given.",
            messages=[Message.user("Please list my 3 most recent orders.")],
            tools=[DUMMY_TOOL],
            max_tokens=256,
        )
        proposed = [(u.name, u.input) for u in out.tool_uses]
        print(
            f"2. tool call   ok in {time.monotonic() - t0:.1f}s: stop_reason={out.stop_reason} "
            f"proposed={proposed or 'nothing (the model answered in text)'}"
        )
    except LLMError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
