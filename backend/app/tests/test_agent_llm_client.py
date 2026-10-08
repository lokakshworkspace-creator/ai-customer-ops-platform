"""GeminiLLMClient against a mock HTTP transport (no network, no real key), and the factory.

The REAL Gemini API is not called by any test. The client is verified against the request and
response shapes in Google's documentation using httpx.MockTransport; `scripts/llm_smoke.py`
(needs your own GEMINI_API_KEY and LLM_MODEL) is the only thing that talks to the real service.
"""

import json

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.agent.llm.base import (
    LLMConfigError,
    LLMUnavailable,
    Message,
    TextBlock,
    ToolDef,
    ToolResultBlock,
    ToolUseBlock,
)
from app.agent.llm.factory import build_llm_client
from app.agent.llm.fake import ObedientFakeLLM
from app.agent.llm.gemini import MISCONFIGURED, UNAVAILABLE, GeminiLLMClient
from app.core.config import Settings

KEY = "AIzaSy-SENTINEL-0123456789abcdef"
MODEL = "test-flash-lite-model"  # whatever LLM_MODEL says: the client never hard-codes one
STRONG = "x" * 40
TOOLS = (
    ToolDef(
        "list_orders",
        "List orders.",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"limit": {"type": "integer", "description": "How many."}},
        },
    ),
)


def ok_body(parts=None, finish="STOP", usage=(11, 7)) -> dict:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts or [{"text": "hello"}]},
                "finishReason": finish,
            }
        ],
        "usageMetadata": {"promptTokenCount": usage[0], "candidatesTokenCount": usage[1]},
    }


class Server:
    """A mock Gemini API: answers from `responses` in order (the last one repeats)."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses) or [httpx.Response(200, json=ok_body())]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)

    def client(self) -> GeminiLLMClient:
        return GeminiLLMClient(
            SecretStr(KEY),
            model=MODEL,
            timeout_s=5,
            retry_delay_s=0,
            transport=httpx.MockTransport(self),
        )


def error(status: int, body: str = "nope") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": body}})


async def complete(client: GeminiLLMClient, messages=None, tools=TOOLS, max_tokens=321):
    return await client.complete(
        system="be careful",
        messages=messages or [Message.user("hi")],
        tools=tools,
        max_tokens=max_tokens,
    )


# --- request mapping ----------------------------------------------------------------------------


async def test_the_request_follows_the_documented_shape_and_the_key_is_only_in_a_header() -> None:
    server = Server()
    await complete(server.client())
    req = server.requests[0]
    assert req.method == "POST"
    assert str(req.url) == (
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    )
    assert req.headers["x-goog-api-key"] == KEY and KEY not in str(req.url)  # never in the URL
    body = server.body
    assert body["systemInstruction"] == {"parts": [{"text": "be careful"}]}
    assert body["generationConfig"] == {"maxOutputTokens": 321}  # sent on every call
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    (tool,) = body["tools"]
    (decl,) = tool["functionDeclarations"]
    assert decl["name"] == "list_orders" and decl["description"] == "List orders."
    assert decl["parameters"]["type"] == "OBJECT"  # Gemini's Schema: upper-case types
    assert "additionalProperties" not in decl["parameters"]  # not part of Gemini's subset
    assert KEY not in json.dumps(body)


async def test_no_tools_key_when_there_are_no_tools() -> None:
    server = Server()
    await complete(server.client(), tools=())
    assert "tools" not in server.body


async def test_a_conversation_with_tool_calls_maps_to_model_and_function_response_turns() -> None:
    server = Server()
    history = [
        Message.user("refund me"),
        Message.assistant(
            TextBlock(""),  # empty text parts are dropped
            ToolUseBlock("call-1", "list_orders", {"limit": 5}, signature="c2lnLWFiYw=="),
            ToolUseBlock("local-abc", "get_order", {"order_id": "o1"}),  # the API sent no id
        ),
        Message.tool(
            ToolResultBlock("call-1", '{"status":"succeeded","data":{"orders":[]}}'),
            ToolResultBlock("local-abc", "plain text, not json", is_error=True),
        ),
        Message.assistant(),  # empty messages are dropped
        Message.assistant(TextBlock("Here you go.")),
    ]
    await complete(server.client(), history)
    assert server.body["contents"] == [
        {"role": "user", "parts": [{"text": "refund me"}]},
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {"name": "list_orders", "args": {"limit": 5}, "id": "call-1"},
                    "thoughtSignature": "c2lnLWFiYw==",  # echoed back unchanged
                },
                {
                    "functionCall": {"name": "get_order", "args": {"order_id": "o1"}}
                },  # no made-up id
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": "list_orders",
                        "response": {"status": "succeeded", "data": {"orders": []}},
                        "id": "call-1",
                    }
                },
                {
                    "functionResponse": {
                        "name": "get_order",  # matched to its call by name
                        "response": {"output": "plain text, not json"},
                    }
                },
            ],
        },
        {"role": "model", "parts": [{"text": "Here you go."}]},
    ]


# --- response mapping ---------------------------------------------------------------------------


async def test_text_and_function_calls_are_read_from_the_candidate() -> None:
    server = Server(
        httpx.Response(
            200,
            json=ok_body(
                [
                    {"text": "I will check.", "thought": False},
                    {
                        "text": "(internal thought summary)",
                        "thought": True,
                    },  # not part of the answer
                    {
                        "functionCall": {"id": "fc-9", "name": "list_orders", "args": {"limit": 3}},
                        "thoughtSignature": "c2ln",
                    },
                    {"functionCall": {"name": "get_order", "args": {}}},  # no id from the API
                ],
                usage=(120, 33),
            ),
        )
    )
    out = await complete(server.client())
    assert out.text == "I will check." and out.stop_reason == "tool_use"
    first, second = out.tool_uses
    assert first == ToolUseBlock("fc-9", "list_orders", {"limit": 3}, "c2ln")
    assert (
        second.name == "get_order" and second.id.startswith("local-") and second.signature is None
    )
    assert (out.usage.input_tokens, out.usage.output_tokens) == (120, 33)
    assert server.client().name == "gemini"


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ("STOP", "end_turn"),
        ("MAX_TOKENS", "max_tokens"),
        ("SAFETY", "end_turn"),
        ("MALFORMED_FUNCTION_CALL", "end_turn"),
    ],
)
async def test_finish_reasons_map_to_stop_reasons(finish: str, expected: str) -> None:
    server = Server(httpx.Response(200, json=ok_body(finish=finish)))
    assert (await complete(server.client())).stop_reason == expected


async def test_a_blocked_prompt_with_no_candidates_is_an_empty_reply_not_a_crash() -> None:
    server = Server(httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    out = await complete(server.client())
    assert out.text == "" and out.tool_uses == [] and out.stop_reason == "end_turn"


# --- failures: timeouts and ONE retry -----------------------------------------------------------


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_a_transient_error_is_retried_exactly_once_then_succeeds(status: int) -> None:
    server = Server(error(status), httpx.Response(200, json=ok_body()))
    out = await complete(server.client())
    assert out.text == "hello" and len(server.requests) == 2


@pytest.mark.parametrize(
    "failure",
    [error(500), error(503), error(429), httpx.ConnectError("down"), httpx.ReadTimeout("slow")],
)
async def test_two_failures_in_a_row_give_a_clear_user_safe_error_after_one_retry(failure) -> None:
    server = Server(failure)  # the last response repeats: every attempt fails
    with pytest.raises(LLMUnavailable) as caught:
        await complete(server.client())
    assert len(server.requests) == 2  # the call and exactly one retry
    assert str(caught.value) == UNAVAILABLE


async def test_retry_after_is_honoured_but_capped() -> None:
    slept: list[float] = []
    server = Server(
        httpx.Response(429, headers={"retry-after": "120"}, json={}),
        httpx.Response(200, json=ok_body()),
    )
    client = server.client()

    import app.agent.llm.gemini as gemini

    real = gemini.asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)

    gemini.asyncio.sleep = fake_sleep
    try:
        await complete(client)
    finally:
        gemini.asyncio.sleep = real
    assert slept == [5.0]  # never wait long inside a request


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_errors_a_retry_cannot_fix_are_not_retried(status: int) -> None:
    server = Server(error(status, f"API key not valid: {KEY}"))  # a server that echoes the key
    with pytest.raises(LLMConfigError) as caught:
        await complete(server.client())
    assert len(server.requests) == 1
    assert str(caught.value) == MISCONFIGURED  # fixed text: the response body is dropped


async def test_a_200_that_is_not_json_is_unavailable() -> None:
    server = Server(httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(LLMUnavailable):
        await complete(server.client())


# --- the key never leaks ------------------------------------------------------------------------


async def test_the_key_never_appears_in_logs_errors_or_repr() -> None:
    bad = Server(error(401, KEY))
    client = bad.client()
    with capture_logs() as logs:
        with pytest.raises(LLMConfigError) as caught:
            await complete(client)
        unreachable = Server(httpx.ConnectError(f"cannot reach host with key {KEY}"))
        with pytest.raises(LLMUnavailable) as caught2:
            await complete(unreachable.client())
        await complete(Server().client())
    everything = (
        json.dumps(logs, default=str) + repr(client) + repr(caught.value) + repr(caught2.value)
    )
    assert KEY not in everything and "SENTINEL" not in everything
    events = [e["event"] for e in logs]
    assert events.count("llm_call_failed") >= 2 and "llm_call" in events
    ok = next(e for e in logs if e["event"] == "llm_call")
    assert ok["input_tokens"] == 11 and ok["model"] == MODEL
    assert "messages" not in ok and "content" not in ok  # no message content in logs


# --- factory ------------------------------------------------------------------------------------


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_the_default_provider_is_the_fake_in_dev() -> None:
    client = build_llm_client(settings())
    assert isinstance(client, ObedientFakeLLM) and client.name == "fake"


def test_gemini_needs_a_key_and_a_model_and_never_falls_back_to_the_fake() -> None:
    with pytest.raises(LLMConfigError, match="GEMINI_API_KEY"):
        build_llm_client(settings(llm_provider="gemini", llm_model=MODEL))
    with pytest.raises(LLMConfigError, match="LLM_MODEL"):
        build_llm_client(settings(llm_provider="gemini", gemini_api_key=KEY))


def test_gemini_with_key_and_model_selects_the_gemini_client() -> None:
    client = build_llm_client(settings(llm_provider="gemini", gemini_api_key=KEY, llm_model=MODEL))
    assert isinstance(client, GeminiLLMClient) and client.name == "gemini"
    assert MODEL in repr(client) and KEY not in repr(client)


@pytest.mark.parametrize("env", ["prod", "test", "staging"])
def test_the_fake_is_refused_outside_dev_instead_of_silently_used(env: str) -> None:
    with pytest.raises(LLMConfigError, match="LLM_PROVIDER"):
        build_llm_client(settings(app_env=env, jwt_secret=STRONG))
