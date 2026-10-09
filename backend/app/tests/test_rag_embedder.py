"""The embedders: the fake (deterministic, no network) and the Gemini client against a mock HTTP
transport. No test calls the real service or uses a real key."""

import json
import math

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.core.config import Settings, get_settings
from app.rag.embedder import (
    FAKE_MODEL,
    MISCONFIGURED,
    UNAVAILABLE,
    DocInput,
    EmbedderConfigError,
    EmbedderUnavailable,
    FakeEmbedder,
    GeminiEmbedder,
    normalize,
)
from app.rag.factory import build_embedder

KEY = "AIzaSy-SENTINEL-embedding-0123456789"
DIM = 8  # tiny vectors keep the mock responses readable


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


# --- the fake -----------------------------------------------------------------------------------


async def test_the_fake_is_deterministic_normalized_and_the_right_size() -> None:
    e = FakeEmbedder(768)
    a = await e.embed_query("How long does shipping take?")
    b = await e.embed_query("How long does shipping take?")
    assert a == b and len(a) == 768
    assert abs(math.sqrt(sum(x * x for x in a)) - 1.0) < 1e-9
    assert e.model == FAKE_MODEL and e.name == "fake"


async def test_the_fake_ranks_word_overlap_above_unrelated_text() -> None:
    e = FakeEmbedder(768)
    q = await e.embed_query("return policy for electronics")
    docs = await e.embed_documents(
        [
            DocInput(
                "Returns policy", "Electronics can be returned within thirty days of delivery."
            ),
            DocInput("Shipping", "Standard delivery takes three to five business days."),
        ]
    )
    assert cosine(q, docs[0]) > cosine(q, docs[1])
    assert cosine(q, docs[0]) > 0.2 and cosine(q, docs[1]) < FakeEmbedder.default_min_cosine


async def test_the_fake_works_on_empty_and_stopword_only_text() -> None:
    e = FakeEmbedder(16)
    v = await e.embed_query("the of and")
    assert len(v) == 16  # a zero vector stays zero (no division by zero)
    assert await e.embed_documents([]) == []


# --- Gemini against a mock transport ------------------------------------------------------------


def reply(n: int, scale: float = 0.5, dim: int = DIM) -> httpx.Response:
    return httpx.Response(
        200,
        json={"embeddings": [{"values": [scale * (i + 1) for i in range(dim)]} for _ in range(n)]},
    )


class Server:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if callable(r):
            r = r(request)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)

    def embedder(self, model="gemini-embedding-001", **kw) -> GeminiEmbedder:
        kw.setdefault("retry_delay_s", 0)
        kw.setdefault("min_interval_s", 0)
        return GeminiEmbedder(
            SecretStr(KEY), model=model, dim=DIM, transport=httpx.MockTransport(self), **kw
        )


def echo(request: httpx.Request) -> httpx.Response:
    n = len(json.loads(request.content)["requests"])
    return reply(n)


async def test_001_documents_use_task_type_title_dimension_and_the_key_stays_in_the_header():
    server = Server(echo)
    e = server.embedder()
    await e.embed_documents([DocInput("Returns policy", "Electronics: 30 days.")])
    req = server.requests[0]
    assert str(req.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents"
    )
    assert req.headers["x-goog-api-key"] == KEY and KEY not in str(req.url)
    (item,) = server.body["requests"]
    assert item == {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": "Electronics: 30 days."}]},
        "taskType": "RETRIEVAL_DOCUMENT",
        "title": "Returns policy",
        "outputDimensionality": DIM,
    }
    assert KEY not in json.dumps(server.body)


async def test_001_queries_use_the_query_task_type_and_no_title() -> None:
    server = Server(echo)
    await server.embedder().embed_query("how do I return a laptop?")
    (item,) = server.body["requests"]
    assert item["taskType"] == "RETRIEVAL_QUERY" and "title" not in item
    assert item["content"] == {"parts": [{"text": "how do I return a laptop?"}]}


async def test_embedding_2_puts_the_task_in_the_text_and_sends_no_task_parameter() -> None:
    server = Server(echo)
    e = server.embedder("gemini-embedding-2")
    await e.embed_documents([DocInput("Returns policy", "30 days."), DocInput("", "No title.")])
    first, second = server.body["requests"]
    assert first["content"]["parts"][0]["text"] == "title: Returns policy | text: 30 days."
    assert second["content"]["parts"][0]["text"] == "title: none | text: No title."
    assert "taskType" not in first and "title" not in first
    await e.embed_query("what is the policy")
    (q,) = server.body["requests"]
    assert q["content"]["parts"][0]["text"] == "task: search result | query: what is the policy"


async def test_one_vector_per_text_in_order_and_each_is_normalized() -> None:
    server = Server(
        httpx.Response(
            200,
            json={
                "embeddings": [
                    {"values": [3.0] + [0.0] * (DIM - 1)},  # 001 returns unnormalized vectors
                    {"values": [0.0, 4.0] + [0.0] * (DIM - 2)},
                ]
            },
        )
    )
    a, b = await server.embedder().embed_documents([DocInput("t", "one"), DocInput("t", "two")])
    assert a[0] == 1.0 and b[1] == 1.0 and len(a) == DIM
    assert normalize([0.0] * 3) == [0.0] * 3


async def test_documents_are_sent_in_batches_with_one_call_per_batch() -> None:
    server = Server(echo)
    e = server.embedder(batch_size=8)
    out = await e.embed_documents([DocInput("t", f"text {i}") for i in range(20)])
    assert len(out) == 20 and len(server.requests) == 3  # 8 + 8 + 4
    assert [len(json.loads(r.content)["requests"]) for r in server.requests] == [8, 8, 4]
    assert e.calls_made == 3


async def test_a_wrong_count_or_dimension_is_refused_not_stored() -> None:
    with pytest.raises(EmbedderConfigError):
        await Server(reply(1)).embedder().embed_documents([DocInput("t", "a"), DocInput("t", "b")])
    with pytest.raises(EmbedderConfigError):
        await Server(reply(1, dim=DIM + 1)).embedder().embed_query("x")


# --- failures -----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_transient_error_is_retried_then_succeeds(status: int) -> None:
    server = Server(httpx.Response(status, json={}), reply(1))
    assert len(await server.embedder().embed_query("x")) == DIM
    assert len(server.requests) == 2


async def test_it_gives_up_after_the_configured_retries_with_a_user_safe_error() -> None:
    server = Server(httpx.Response(429, json={}))
    e = server.embedder(max_retries=3)
    with pytest.raises(EmbedderUnavailable) as caught:
        await e.embed_query("x")
    assert len(server.requests) == 4 and str(caught.value) == UNAVAILABLE and e.calls_made == 4
    for failure in (httpx.ConnectError("down"), httpx.ReadTimeout("slow")):
        s2 = Server(failure)
        with pytest.raises(EmbedderUnavailable):
            await s2.embedder(max_retries=1).embed_query("x")
        assert len(s2.requests) == 2


async def test_retry_after_is_honoured_but_capped_and_backoff_doubles(monkeypatch) -> None:
    import app.rag.embedder as mod

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    server = Server(
        httpx.Response(429, headers={"retry-after": "500"}, json={}),
        httpx.Response(503, json={}),
        httpx.Response(503, json={}),
        reply(1),
    )
    await server.embedder(retry_delay_s=1.0, min_interval_s=0).embed_query("x")
    assert slept == [60.0, 2.0, 4.0]  # capped Retry-After, then 1 s * 2^(attempt-1)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_errors_a_retry_cannot_fix_are_not_retried_and_do_not_echo_the_body(status: int):
    server = Server(httpx.Response(status, json={"error": {"message": f"bad key {KEY}"}}))
    with pytest.raises(EmbedderConfigError) as caught:
        await server.embedder().embed_query("secret text")
    assert len(server.requests) == 1 and str(caught.value) == MISCONFIGURED


async def test_the_key_and_the_text_never_appear_in_logs_errors_or_repr() -> None:
    e = Server(httpx.Response(401, json={"error": {"message": KEY}})).embedder()
    with capture_logs() as logs:
        with pytest.raises(EmbedderConfigError) as c1:
            await e.embed_query("my private question about order 123")
        with pytest.raises(EmbedderUnavailable) as c2:
            await (
                Server(httpx.ConnectError(f"cannot reach {KEY}"))
                .embedder(max_retries=0)
                .embed_query("q")
            )
        await Server(echo).embedder().embed_documents([DocInput("t", "confidential policy text")])
    everything = json.dumps(logs, default=str) + repr(e) + repr(c1.value) + repr(c2.value)
    assert KEY not in everything and "SENTINEL" not in everything
    assert "private question" not in everything and "confidential policy" not in everything
    events = [x["event"] for x in logs]
    assert "embed_call_failed" in events and "embed_call" in events


async def test_calls_are_paced_by_the_minimum_interval(monkeypatch) -> None:
    import app.rag.embedder as mod

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    server = Server(echo)
    e = server.embedder(batch_size=1, min_interval_s=10.0)
    await e.embed_documents([DocInput("t", "a"), DocInput("t", "b")])
    assert len(slept) == 1 and 0 < slept[0] <= 10.0  # the second call waited; the first did not


# --- factory and settings -----------------------------------------------------------------------


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_the_test_session_uses_the_fake_embedder_whatever_the_env_file_says() -> None:
    s = get_settings()
    assert (s.embedding_provider, s.embedding_model) == ("fake", None)


def test_the_default_is_the_fake_in_dev_with_the_configured_dimension() -> None:
    e = build_embedder(settings(embedding_dim=64))
    assert isinstance(e, FakeEmbedder) and e.dim == 64


def test_gemini_needs_a_key_and_a_model_and_never_falls_back_to_the_fake() -> None:
    with pytest.raises(EmbedderConfigError, match="GEMINI_API_KEY"):
        build_embedder(settings(embedding_provider="gemini", embedding_model="m"))
    with pytest.raises(EmbedderConfigError, match="EMBEDDING_MODEL"):
        build_embedder(settings(embedding_provider="gemini", gemini_api_key=KEY))


def test_gemini_with_key_and_model_uses_the_configured_name_and_dimension() -> None:
    e = build_embedder(
        settings(
            embedding_provider="gemini",
            gemini_api_key=KEY,
            embedding_model="some-model",
            embedding_dim=768,
        )
    )
    assert isinstance(e, GeminiEmbedder) and (e.model, e.dim) == ("some-model", 768)
    assert KEY not in repr(e)


@pytest.mark.parametrize("env", ["prod", "test", "staging"])
def test_the_fake_embedder_is_refused_outside_dev(env: str) -> None:
    with pytest.raises(EmbedderConfigError, match="EMBEDDING_PROVIDER"):
        build_embedder(settings(app_env=env, jwt_secret="x" * 40))


def test_embedding_settings_are_validated_and_blank_means_unset() -> None:
    s = settings(embedding_model="  ")
    assert s.embedding_model is None and s.embedding_dim == 768 and s.kb_namespace == "default"
    for bad in (
        {"embedding_dim": 4},
        {"embedding_dim": 3072},
        {"embedding_provider": "claude"},
        {"rag_top_k": 0},
    ):
        with pytest.raises(Exception):  # noqa: B017 (pydantic ValidationError)
            settings(**bad)
