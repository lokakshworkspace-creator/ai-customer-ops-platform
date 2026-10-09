"""Embedders: turn text into unit-length vectors for the knowledge base.

* `FakeEmbedder`: deterministic, hash-based (feature hashing of words), no network. Used by ALL
  tests and by the demo by default. It captures word overlap, nothing more: it is good enough to
  test the plumbing (audience filter, thresholds, atomic ingest) and says NOTHING about how well a
  real embedding model retrieves.
* `GeminiEmbedder`: the Gemini embeddings REST API over plain httpx (no SDK), using
  `batchEmbedContents` with ONE text per request item (a single request with several parts would
  return one aggregated vector, not one per text). The API key goes only in the `x-goog-api-key`
  header; it is a `SecretStr`, never logged, never in an error message or the URL. The model name
  and the dimension come from settings.

Model differences handled here (Google's documentation, checked 2026-10-09, and measured):
`gemini-embedding-001` takes a `taskType` parameter (and an optional `title` for documents) and
returns UNNORMALIZED vectors at reduced dimensions, so we normalize; `gemini-embedding-2` has no
task parameter (the task is part of the text: `task: search result | query: ...` for a query and
`title: ... | text: ...` for a document) and normalizes itself.

Vectors of different models are NOT comparable: the knowledge base stores the model name with every
chunk and retrieval only compares against the current model.
"""

import asyncio
import hashlib
import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
import structlog
from pydantic import SecretStr

log = structlog.get_logger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
FAKE_MODEL = "fake-hash-v1"
UNAVAILABLE = "The knowledge base search is temporarily unavailable."
MISCONFIGURED = "The knowledge base search is not configured correctly."
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class EmbedderError(Exception):
    """Messages are safe to show to a user and never contain secrets or text sent for embedding."""


class EmbedderUnavailable(EmbedderError):
    """Timeouts, rate limits and server errors that kept failing after the retries."""


class EmbedderConfigError(EmbedderError):
    """The service rejected the request in a way retrying will not fix, or the answer was wrong."""


@dataclass(frozen=True, slots=True)
class DocInput:
    title: str
    text: str


class Embedder(Protocol):
    name: Literal["fake", "gemini"]
    model: str
    dim: int
    default_min_cosine: float  # the calibrated "relevant enough" cosine for THIS embedder
    default_min_ts_rank: float
    calls_made: int  # API calls so far (the ingest script caps this)

    async def embed_documents(self, docs: Sequence[DocInput]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm > 0 else list(vector)


# --- the fake ---------------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    (
        "a an and are as at be but by can do does for from how i if in is it its me my of on or "
        "our "
        "so that the their there they this to us was we what when where which who why will with "
        "you your"
    ).split()
)


def _tokens(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower()):
        if w in _STOP or len(w) < 2:
            continue
        for suffix in ("ing", "es", "s", "ed"):  # a crude stemmer: enough for word overlap
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out


class FakeEmbedder:
    name = "fake"
    default_min_cosine = 0.4  # fake vectors are lexical: stop words alone give ~0.3
    default_min_ts_rank = 0.12  # Postgres FTS, so the same for every embedder; see rag/eval.py

    def __init__(self, dim: int = 768) -> None:
        self.model = FAKE_MODEL
        self.dim = dim
        self.calls_made = 0

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = _tokens(text)
        features = [(t, 1.0) for t in toks] + [
            (f"{a}_{b}", 0.5) for a, b in zip(toks, toks[1:], strict=False)
        ]
        for feature, weight in features:
            h = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[index] += sign * weight
        return normalize(vec)

    async def embed_documents(self, docs: Sequence[DocInput]) -> list[list[float]]:
        self.calls_made += 1 if docs else 0
        return [self._embed(f"{d.title} {d.text}") for d in docs]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def __repr__(self) -> str:
        return f"FakeEmbedder(dim={self.dim})"


# --- Gemini -----------------------------------------------------------------------------------


def is_gemini_2(model: str) -> bool:
    return "embedding-2" in model


class GeminiEmbedder:
    name = "gemini"
    # Starting points; the golden-question run calibrates them for the real models.
    default_min_cosine = 0.55
    default_min_ts_rank = 0.12  # Postgres FTS, so the same for every embedder; see rag/eval.py

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str,
        dim: int,
        timeout_s: float = 30.0,
        batch_size: int = 8,
        min_interval_s: float = 0.3,
        max_retries: int = 5,
        retry_delay_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.model = model
        self.dim = dim
        self.calls_made = 0
        self._batch_size = batch_size
        self._min_interval_s = min_interval_s
        self._max_retries = max_retries
        self._retry_delay_s = retry_delay_s
        self._url = f"{base_url}/models/{model}:batchEmbedContents"
        self._last_call = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={"x-goog-api-key": api_key.get_secret_value()},
            transport=transport,
        )

    def __repr__(self) -> str:  # never anything secret
        return f"GeminiEmbedder(model={self.model!r}, dim={self.dim})"

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- request building ----------------------------------------------------------------------

    def _item(self, text: str, *, query: bool, title: str | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "model": f"models/{self.model}",
            "outputDimensionality": self.dim,
        }
        if is_gemini_2(self.model):  # the task is part of the text
            shown = (
                f"task: search result | query: {text}"
                if query
                else f"title: {title or 'none'} | text: {text}"
            )
            item["content"] = {"parts": [{"text": shown}]}
        else:
            item["content"] = {"parts": [{"text": text}]}
            item["taskType"] = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
            if title and not query:
                item["title"] = title
        return item

    # -- public ----------------------------------------------------------------------------------

    async def embed_documents(self, docs: Sequence[DocInput]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(docs), self._batch_size):
            batch = docs[i : i + self._batch_size]
            out += await self._call([self._item(d.text, query=False, title=d.title) for d in batch])
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self._call([self._item(text, query=True)]))[0]

    # -- one HTTP call, with pacing and retries --------------------------------------------------

    async def _call(self, items: list[dict[str, Any]]) -> list[list[float]]:
        wait = self._min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        started = time.monotonic()
        data = await self._post({"requests": items}, started)
        self._last_call = time.monotonic()
        embeddings = data.get("embeddings") or []
        if len(embeddings) != len(items):
            raise EmbedderConfigError(MISCONFIGURED)  # one vector per item, or something is wrong
        vectors = []
        for e in embeddings:
            values = e.get("values")
            if not isinstance(values, list) or len(values) != self.dim:
                raise EmbedderConfigError(MISCONFIGURED)  # wrong dimension: never store it
            vectors.append(normalize([float(x) for x in values]))
        log.info(
            "embed_call",
            embedder="gemini",
            model=self.model,
            items=len(items),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return vectors

    async def _post(self, body: dict[str, Any], started: float) -> dict[str, Any]:
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            self.calls_made += 1
            try:
                resp = await self._client.post(self._url, json=body)
            except httpx.TransportError as exc:  # timeouts, connection failures
                self._log_failure(type(exc).__name__, None, started)
                if attempt == attempts:
                    raise EmbedderUnavailable(UNAVAILABLE) from None
                await asyncio.sleep(self._backoff(attempt, None))
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    self._log_failure("InvalidJSON", 200, started)
                    raise EmbedderUnavailable(UNAVAILABLE) from None
            self._log_failure("HTTPStatusError", resp.status_code, started)
            if resp.status_code in TRANSIENT_STATUS:
                if attempt == attempts:
                    raise EmbedderUnavailable(UNAVAILABLE)
                await asyncio.sleep(self._backoff(attempt, resp))
                continue
            raise EmbedderConfigError(MISCONFIGURED)  # 400 / 401 / 403 / 404: retrying cannot help
        raise EmbedderUnavailable(UNAVAILABLE)  # pragma: no cover

    def _backoff(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            try:
                return min(max(float(resp.headers.get("retry-after", "")), 0.0), 60.0)
            except ValueError:
                pass
        return min(self._retry_delay_s * (2 ** (attempt - 1)), 30.0)  # 1, 2, 4, 8, 16 s ...

    def _log_failure(self, error: str, status: int | None, started: float) -> None:
        # The class name and status only: an error body can echo what we sent.
        log.warning(
            "embed_call_failed",
            embedder="gemini",
            model=self.model,
            error=error,
            status_code=status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
