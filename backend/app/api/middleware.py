import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
# fullmatch, not ^...$: "$" would also accept a trailing newline
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")

log = structlog.get_logger(__name__)


class SecurityHeadersMiddleware:
    """Two response headers for the API, whoever calls it (the Next.js page's own headers are set in
    frontend/next.config.ts):

    * `X-Content-Type-Options: nosniff` on every response: a browser must not guess that a JSON body
      is something else.
    * `Cache-Control: no-store` on everything under `/api/`, unless the endpoint set its own: those
      responses carry bearer tokens (login) and personal data (orders, refunds, approvals), and a
      shared cache or a browser's back/forward cache must not keep them.

    There is deliberately no CORS middleware and no CORS header anywhere: the browser only talks to
    the Next.js origin, which proxies /api/*, so a page on any other origin cannot read a response
    (tests pin that). HSTS is not set: the app is plain HTTP, and the header belongs where TLS
    ends."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = scope["path"].startswith("/api/")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                if is_api and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RequestIDMiddleware:
    """Assigns a request_id, binds it to every log line for the request, echoes it in the
    response header, and logs one access line. An inbound X-Request-ID is honoured only if
    it matches [A-Za-z0-9_-]{8,64}; anything else is replaced with a fresh one. An unhandled error
    is logged here (with its traceback and the id) and answered with a generic 500 that carries the
    id, never the exception's text."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = inbound if _VALID_REQUEST_ID.fullmatch(inbound) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500
        response_started = False
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # An unhandled error. Starlette would build its own 500 OUTSIDE this middleware, so the
            # response would carry no request id and the crash would be logged without one. Log it
            # here, while the id is bound, and answer with a clean 500 that names the id: a user
            # (or a support ticket) can quote it and an operator can find the log lines.
            log.error("unhandled_exception", exc_info=True)
            if response_started:
                raise  # the response is already on its way: nothing cleaner can be sent
            status_code = 500
            await JSONResponse(
                {"detail": "Internal server error", "request_id": request_id},
                status_code=500,
                headers={REQUEST_ID_HEADER: request_id},
            )(scope, receive, send)
        finally:
            log.info(
                "request",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()
