"""Turn a ToolOutcome into an HTTP response. One mapping for every tool-backed endpoint.

Success-shaped (a result exists, even if a payout then failed):  <data> + policy + tool_call_id
Error-shaped:  {"detail": <human text>, "code": <machine code>, "tool_call_id": ...}
"""

from typing import Any

from fastapi.responses import JSONResponse

from app.db.models.enums import ToolCallStatus
from app.tools.executor import ToolOutcome

REPLAY_HEADER = "Idempotent-Replayed"


def tool_response(outcome: ToolOutcome) -> JSONResponse:
    headers: dict[str, str] = {}

    # A result was produced (including "created, but the payout failed / is unconfirmed").
    if outcome.data is not None and outcome.status != ToolCallStatus.denied:
        body: dict[str, Any] = dict(outcome.data)
        body["tool_call_id"] = str(outcome.tool_call_id)
        if outcome.error_code:  # e.g. PAYOUT_FAILED: the refund exists, its status says failed
            body["error"] = {"code": outcome.error_code, "message": outcome.error}
        if outcome.data.get("replayed"):
            headers[REPLAY_HEADER] = "true"
        return JSONResponse(body, status_code=outcome.http_status, headers=headers)

    body = {"detail": outcome.reason or outcome.error, "tool_call_id": str(outcome.tool_call_id)}
    if outcome.status == ToolCallStatus.denied:
        body["code"] = outcome.reason_code
        body["decision"] = "DENY"
    else:
        body["code"] = outcome.error_code
        if outcome.details:
            body["detail"] = outcome.details
    return JSONResponse(body, status_code=outcome.http_status)
