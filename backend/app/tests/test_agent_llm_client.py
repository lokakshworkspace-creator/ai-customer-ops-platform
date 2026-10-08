"""ClaudeLLMClient against a mock HTTP transport (no network, no real key), and the factory.

The Anthropic SDK 1.x runs on `httpx2`, so the mock transport comes from there.
"""

import json

import httpx2
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
from app.agent.llm.claude import MISCONFIGURED, UNAVAILABLE, ClaudeLLMClient
from app.agent.llm.factory import build_llm_client
from app.agent.llm.fake import ObedientFakeLLM
from app.core.config import Settings

KEY = "sk-ant-SENTINEL-0123456789abcdef"
STRONG = "x" * 40
TOOLS = (ToolDef("list_orders", "List orders.", {"type": "object", "properties": {}}),)


def ok_body(content=None, stop="end_turn", usage=(11, 7)) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content or [{"type": "text", "text": "hello"}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }


class Server:
    """A mock Anthropic API: answers from `responses` in order (the last one repeats)."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses) or [httpx2.Response(200, json=ok_body())]
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)

    def client(self, **kwargs) -> ClaudeLLMClient:
        return ClaudeLLMClient(
            SecretStr(KEY),
            model="claude-sonnet-5-5",
            timeout_s=5,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self)),
            **kwargs,
        )


def fast_error(status: int, **extra) -> httpx2.Response:
    # retry-after-ms makes the SDK's single retry immediate instead of ~0.5 s
    return httpx2.Response(
        status,
        json={"type": "error", "error": {"type": "api_error", "message": "nope"}},
        headers={"retry-after-ms": "1"},
        **extra,
    )


async def complete(client: ClaudeLLMClient, messages=None, tools=TOOLS, max_tokens=321):
    return await client.complete(
        system="be careful",
        messages=messages or [Message.user("hi")],
        tools=tools,
        max_tokens=max_tokens,
    )


# --- mapping ------------------------------------------------------------------------------------


async def test_the_request_carries_model_max_tokens_system_tools_and_the_key() -> None:
    server = Server()
    await complete(server.client())
    body = server.body
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 321
    assert body["system"] == "be careful"
    assert body["tools"] == [
        {
            "name": "list_orders",
            "description": "List orders.",
            "input_schema": TOOLS[0].input_schema,
        }
    ]
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert server.requests[0].headers["x-api-key"] == KEY  # it is used...


async def test_messages_are_mapped_and_tool_results_travel_in_a_user_message() -> None:
    server = Server()
    history = [
        Message.user("refund me"),
        Message.assistant(TextBlock(""), ToolUseBlock("toolu_1", "list_orders", {"limit": 5})),
        Message.tool(ToolResultBlock("toolu_1", '{"status":"succeeded"}')),
        Message.assistant(),  # empty messages are dropped
        Message.tool(ToolResultBlock("toolu_2", "boom", is_error=True)),
    ]
    await complete(server.client(), history)
    assert server.body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "refund me"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "list_orders", "input": {"limit": 5}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"status":"succeeded"}',
                    "is_error": False,
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_2",
                    "content": "boom",
                    "is_error": True,
                }
            ],
        },
    ]


async def test_no_tools_key_when_there_are_no_tools() -> None:
    server = Server()
    await complete(server.client(), tools=())
    assert "tools" not in server.body


async def test_the_response_is_mapped() -> None:
    server = Server(
        httpx2.Response(
            200,
            json=ok_body(
                [
                    {"type": "text", "text": "Looking."},
                    {
                        "type": "tool_use",
                        "id": "toolu_9",
                        "name": "list_orders",
                        "input": {"limit": 3},
                    },
                ],
                stop="tool_use",
                usage=(120, 33),
            ),
        )
    )
    out = await complete(server.client())
    assert out.text == "Looking." and out.stop_reason == "tool_use"
    assert out.tool_uses == [ToolUseBlock("toolu_9", "list_orders", {"limit": 3})]
    assert (out.usage.input_tokens, out.usage.output_tokens) == (120, 33)
    assert server.client().name == "claude"


# --- failures: timeouts and ONE retry -----------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503, 529])
async def test_a_transient_error_is_retried_exactly_once_then_succeeds(status: int) -> None:
    server = Server(fast_error(status), httpx2.Response(200, json=ok_body()))
    out = await complete(server.client())
    assert out.text == "hello" and len(server.requests) == 2


@pytest.mark.parametrize("failure", [fast_error(500), fast_error(529), httpx2.ConnectError("down")])
async def test_two_failures_in_a_row_give_a_clear_user_safe_error_after_one_retry(failure) -> None:
    server = Server(failure)  # the last response repeats: every attempt fails
    with pytest.raises(LLMUnavailable) as caught:
        await complete(server.client())
    assert len(server.requests) == 2  # the call and exactly one retry
    assert str(caught.value) == UNAVAILABLE


async def test_a_timeout_is_a_transient_failure_too() -> None:
    server = Server(httpx2.ReadTimeout("slow"))
    with pytest.raises(LLMUnavailable):
        await complete(server.client())
    assert len(server.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_errors_a_retry_cannot_fix_are_not_retried(status: int) -> None:
    echoed = httpx2.Response(
        status, json={"type": "error", "error": {"type": "x", "message": f"bad key {KEY}"}}
    )
    server = Server(echoed)
    with pytest.raises(LLMConfigError) as caught:
        await complete(server.client())
    assert len(server.requests) == 1
    assert (
        str(caught.value) == MISCONFIGURED
    )  # fixed text: the SDK message (which echoed the key) is dropped


# --- the key never leaks ------------------------------------------------------------------------


async def test_the_key_never_appears_in_logs_errors_or_repr() -> None:
    server = Server(
        httpx2.Response(401, json={"type": "error", "error": {"type": "x", "message": KEY}})
    )
    client = server.client()
    with capture_logs() as logs:
        with pytest.raises(LLMConfigError) as caught:
            await complete(client)
        ok_client = Server().client()
        await complete(ok_client)
    everything = (
        json.dumps(logs, default=str) + repr(client) + str(caught.value) + repr(caught.value)
    )
    assert KEY not in everything and "SENTINEL" not in everything
    events = [e["event"] for e in logs]
    assert "llm_call_failed" in events and "llm_call" in events
    ok = next(e for e in logs if e["event"] == "llm_call")
    assert ok["input_tokens"] == 11 and ok["model"] == "claude-sonnet-5-5"
    assert "messages" not in ok and "content" not in ok  # no message content in logs


# --- factory --------------------------------------------------------------------------------------


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_a_key_selects_claude() -> None:
    client = build_llm_client(settings(anthropic_api_key=KEY, anthropic_model="claude-sonnet-5-5"))
    assert isinstance(client, ClaudeLLMClient) and client.name == "claude"


def test_no_key_in_dev_selects_the_fake() -> None:
    client = build_llm_client(settings())
    assert isinstance(client, ObedientFakeLLM) and client.name == "fake"


@pytest.mark.parametrize("env", ["prod", "test", "staging"])
def test_no_key_outside_dev_refuses_instead_of_faking(env: str) -> None:
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        build_llm_client(settings(app_env=env, jwt_secret=STRONG))
