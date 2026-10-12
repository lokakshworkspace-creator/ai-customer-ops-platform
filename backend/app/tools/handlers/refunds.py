"""Refund tools: request_refund (customers and admins), approve_refund / reject_refund (admins).

Lock order is defined in app.db.locking: order -> user -> refund -> approval.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import locking
from app.db.models import Approval, Order, Refund
from app.db.models.enums import ApprovalStatus, PolicyDecision, RefundStatus, ToolCallStatus
from app.events.types import EventType
from app.modules import audit
from app.modules.orders import service as orders_service
from app.modules.refunds import expiry, repo
from app.modules.refunds import service as refund_service
from app.modules.refunds.hashing import is_valid_idempotency_key, request_hash
from app.modules.refunds.payments import PaymentProvider
from app.modules.refunds.service import PayoutOutcome, payout_flow
from app.policy import (
    APPROVE_REFUND,
    REJECT_REFUND,
    evaluate_refund_decision,
    evaluate_refund_request,
)
from app.policy.rules import Rules
from app.policy.types import PolicyResult, ReasonCode
from app.tools.context import ToolContext
from app.tools.handlers.base import (
    AfterCommit,
    AfterCommitResult,
    Decision,
    ToolError,
    ToolResult,
)
from app.tools.schemas import ApproveRefundInput, RejectRefundInput, RequestRefundInput


def policy_json(policy: PolicyResult) -> dict[str, Any]:
    return {
        "decision": policy.decision.value,
        "reason_code": policy.reason_code.value,
        "reason": policy.reason,
        "all_reason_codes": [c.value for c in policy.all_reason_codes],
    }


def response_data(
    refund: Refund, policy: PolicyResult, *, replayed: bool, approval_id: uuid.UUID | None
) -> dict[str, Any]:
    return {
        "refund": refund_service.refund_json(refund),
        "policy": policy_json(policy),
        "approval_id": str(approval_id) if approval_id else None,
        "replayed": replayed,
    }


def payout_result(out: PayoutOutcome) -> AfterCommitResult:
    """Map the payout result onto the tool call. A declined payout is still a created refund:
    the HTTP status stays the handler's (201 / 200) and the body shows refund.status = failed."""
    data = {"refund": out.refund}
    status = out.refund["status"]
    if out.kind == "unconfirmed":
        return AfterCommitResult(
            status=ToolCallStatus.failed,
            data=data,
            http_status=202,
            error_code="PAYOUT_UNCONFIRMED",
            error="The payment provider did not confirm the payout; it will be retried.",
        )
    if status == RefundStatus.failed.value:
        return AfterCommitResult(
            status=ToolCallStatus.failed, data=data, error_code="PAYOUT_FAILED", error=out.error
        )
    return AfterCommitResult(status=ToolCallStatus.succeeded, data=data)


def make_payout_hook(
    provider: PaymentProvider, refund_id: uuid.UUID, request_id: str | None
) -> AfterCommit:
    async def hook(session: AsyncSession) -> AfterCommitResult:
        return payout_result(await payout_flow(session, provider, refund_id, request_id=request_id))

    return hook


# --- request_refund ----------------------------------------------------------------------


@dataclass(slots=True)
class _RequestState:
    key: str
    req_hash: str
    customer_id: uuid.UUID | None = None
    currency: str = ""
    payment_id: uuid.UUID | None = None
    replay: Refund | None = None  # set when this is an idempotent replay


def _replay(existing: Refund, req_hash: str, key: str) -> Decision:
    """Same key, same payload -> the original result. Same key, different payload -> 409."""
    if existing.request_hash != req_hash:
        raise ToolError(
            "IDEMPOTENCY_KEY_REUSED",
            "This Idempotency-Key was already used with a different request payload.",
            http_status=409,
        )
    policy = PolicyResult(
        PolicyDecision.ALLOW,
        ReasonCode.IDEMPOTENT_REPLAY,
        "Same Idempotency-Key and payload: returning the original result.",
        (ReasonCode.IDEMPOTENT_REPLAY,),
    )
    return Decision(policy, state=_RequestState(key, req_hash, replay=existing))


class RequestRefundHandler:
    def __init__(self, provider: PaymentProvider) -> None:
        self.provider = provider

    async def decide(
        self, session: AsyncSession, ctx: ToolContext, args: RequestRefundInput, rules: Rules
    ) -> Decision:
        key = ctx.idempotency_key
        if not is_valid_idempotency_key(key):
            raise ToolError(
                "IDEMPOTENCY_KEY_INVALID",
                "A valid Idempotency-Key is required (8-255 characters of A-Z a-z 0-9 . _ : -).",
                http_status=400,
            )
        assert key is not None

        # 1. order, 2. user (the lock order). Owner-scoped: not yours == not found.
        order = await locking.lock_order(
            session, args.order_id, owner_id=orders_service.owner_filter(ctx.actor)
        )
        if order is None:
            raise ToolError("NOT_FOUND", "Order not found", http_status=404)
        await locking.lock_user(session, order.user_id)

        req_hash = request_hash(
            order_id=args.order_id, amount=args.amount, reason_code=args.reason_code, note=args.note
        )
        # Checked under the locks, so a concurrent identical request has either fully committed
        # (we replay it) or is waiting behind us.
        existing = await repo.get_by_user_key(session, ctx.actor.id, key)
        if existing is not None:
            return _replay(existing, req_hash, key)

        payment = await repo.succeeded_payment(session, order.id)
        facts = await refund_service.build_facts(
            session,
            rules,
            order,
            has_payment=payment is not None,
            amount=args.amount,
            reason_code=args.reason_code,
            now=ctx.now,
        )
        policy = evaluate_refund_request(rules, ctx.actor, facts, ctx.now)
        state = _RequestState(
            key, req_hash, order.user_id, order.currency, payment.id if payment else None
        )
        return Decision(policy, state=state)

    async def execute(
        self, session: AsyncSession, ctx: ToolContext, args: RequestRefundInput, decision: Decision
    ) -> ToolResult:
        st: _RequestState = decision.state
        if st.replay is not None:
            return await self._replayed(session, st.replay, decision.policy)

        needs_approval = decision.policy.needs_approval
        assert st.payment_id is not None  # DENY (PAYMENT_NOT_FOUND) would have stopped us
        refund_id = uuid.uuid4()
        inserted = await repo.insert_idempotent(
            session,
            {
                "id": refund_id,
                "idempotency_key": st.key,
                "request_hash": st.req_hash,
                "payment_id": st.payment_id,
                "order_id": args.order_id,
                "requested_by": ctx.actor.id,
                "amount": args.amount,
                "currency": st.currency,
                "reason_code": args.reason_code,
                "status": RefundStatus.pending_approval
                if needs_approval
                else RefundStatus.processing,
                "reason": args.note,
            },
        )
        if inserted is None:
            # Lost a race on the UNIQUE key to a request that holds a different order lock (the
            # same key used on another order). The database arbitrates; we replay or conflict.
            existing = await repo.get_by_user_key(session, ctx.actor.id, st.key)
            if existing is None:  # pragma: no cover - the winner is committed by now
                raise ToolError("CONFLICT", "Concurrent request; please retry.", http_status=409)
            return await self._replayed(
                session, existing, _replay(existing, st.req_hash, st.key).policy
            )

        refund = await session.get(Refund, refund_id, populate_existing=True)
        assert refund is not None
        refund_service.emit(
            session, refund, st.customer_id, EventType.REFUND_REQUESTED, request_id=ctx.request_id
        )
        audit.record(
            session,
            actor_id=ctx.actor.id,
            action="refund.requested",
            resource_type="refund",
            resource_id=refund.id,
            decision=decision.policy.decision,
            reason_code=decision.policy.reason_code.value,
            reason=decision.policy.reason,
            details={"amount": refund_service.money(refund.amount), "order_id": refund.order_id},
            request_id=ctx.request_id,
        )

        approval_id: uuid.UUID | None = None
        after_commit: AfterCommit | None = None
        if needs_approval:
            approval = Approval(
                refund_id=refund.id,
                tool_call_id=ctx.tool_call_id,
                requested_by=ctx.actor.id,
                action="request_refund",
                payload={
                    "order_id": str(refund.order_id),
                    "amount": refund_service.money(refund.amount),
                    "currency": refund.currency,
                    "reason_code": refund.reason_code,
                    "reason_codes": [c.value for c in decision.policy.all_reason_codes],
                    "reason": decision.policy.reason,
                },
                status=ApprovalStatus.pending,
            )
            session.add(approval)
            await session.flush()
            approval_id = approval.id
        else:
            refund_service.emit(
                session,
                refund,
                st.customer_id,
                EventType.REFUND_APPROVED,
                request_id=ctx.request_id,
                auto=True,
                approved_by=None,
            )
            after_commit = make_payout_hook(self.provider, refund.id, ctx.request_id)

        return ToolResult(
            data=response_data(refund, decision.policy, replayed=False, approval_id=approval_id),
            http_status=202 if needs_approval else 201,
            after_commit=after_commit,
        )

    async def _replayed(
        self, session: AsyncSession, existing: Refund, policy: PolicyResult
    ) -> ToolResult:
        approval = await repo.latest_approval_for(session, existing.id)
        return ToolResult(
            data=response_data(
                existing, policy, replayed=True, approval_id=approval.id if approval else None
            ),
            http_status=200,
        )


# --- approve / reject --------------------------------------------------------------------


@dataclass(slots=True)
class _DecisionState:
    approval: Approval
    refund: Refund
    customer_id: uuid.UUID


async def _lock_for_decision(
    session: AsyncSession, approval_id: uuid.UUID, *, now: datetime
) -> tuple[Approval, Refund, Order]:
    """Take the locks in the documented order. We must read the approval and refund first, WITHOUT
    locking, only to learn which order to lock; everything is then re-read under the locks."""
    approval0 = await session.get(Approval, approval_id)
    if approval0 is None:
        raise ToolError("NOT_FOUND", "Approval not found", http_status=404)
    if approval0.refund_id is None:
        raise ToolError(
            "NOT_A_REFUND_APPROVAL", "This approval is not for a refund.", http_status=409
        )
    refund0 = await session.get(Refund, approval0.refund_id)
    assert refund0 is not None

    order = await locking.lock_order(session, refund0.order_id)  # admin: unscoped
    assert order is not None
    await locking.lock_user(session, order.user_id)
    refund = await locking.lock_refund(session, refund0.id)
    approval = await locking.lock_approval(session, approval_id)
    assert refund is not None and approval is not None

    # Expired: swept already (status `expired`), or past the deadline and not swept yet. Either way
    # nobody may decide it any more (approve and reject alike).
    if approval.status == ApprovalStatus.expired or expiry.is_past_deadline(approval, now):
        raise ToolError(
            "APPROVAL_EXPIRED", "This approval expired: nobody decided it in time.", http_status=409
        )
    if approval.status != ApprovalStatus.pending or refund.status != RefundStatus.pending_approval:
        raise ToolError("ALREADY_DECIDED", "This approval was already decided.", http_status=409)
    return approval, refund, order


class _DecisionHandler:
    action: str

    async def decide(
        self, session: AsyncSession, ctx: ToolContext, args: Any, rules: Rules
    ) -> Decision:
        approval, refund, order = await _lock_for_decision(session, args.approval_id, now=ctx.now)
        customer_id = order.user_id
        payment = await repo.succeeded_payment(session, order.id)
        facts = await refund_service.build_facts(
            session,
            rules,
            order,
            has_payment=payment is not None,
            amount=refund.amount,
            reason_code=refund.reason_code,
            now=ctx.now,
            exclude_refund_id=refund.id,
        )
        policy = evaluate_refund_decision(
            rules,
            self.action,
            ctx.actor,
            requested_by=refund.requested_by,
            refund_facts=facts,
            now=ctx.now,
        )
        return Decision(policy, state=_DecisionState(approval, refund, customer_id))


class ApproveRefundHandler(_DecisionHandler):
    action = APPROVE_REFUND

    def __init__(self, provider: PaymentProvider) -> None:
        self.provider = provider

    async def execute(
        self, session: AsyncSession, ctx: ToolContext, args: ApproveRefundInput, decision: Decision
    ) -> ToolResult:
        st: _DecisionState = decision.state
        st.approval.status = ApprovalStatus.approved
        st.approval.decided_by = ctx.actor.id
        st.approval.decided_at = ctx.now
        st.approval.decision_reason = args.note
        st.refund.status = RefundStatus.processing
        await session.flush()
        refund_service.emit(
            session,
            st.refund,
            st.customer_id,
            EventType.REFUND_APPROVED,
            request_id=ctx.request_id,
            auto=False,
            approved_by=ctx.actor.id,
        )
        audit.record(
            session,
            actor_id=ctx.actor.id,
            action="refund.approved",
            resource_type="refund",
            resource_id=st.refund.id,
            decision=decision.policy.decision,
            reason_code=decision.policy.reason_code.value,
            reason=args.note,
            details={"approval_id": st.approval.id},
            request_id=ctx.request_id,
        )
        return ToolResult(
            data=response_data(
                st.refund, decision.policy, replayed=False, approval_id=st.approval.id
            ),
            http_status=200,
            after_commit=make_payout_hook(self.provider, st.refund.id, ctx.request_id),
        )


class RejectRefundHandler(_DecisionHandler):
    action = REJECT_REFUND

    async def execute(
        self, session: AsyncSession, ctx: ToolContext, args: RejectRefundInput, decision: Decision
    ) -> ToolResult:
        st: _DecisionState = decision.state
        st.approval.status = ApprovalStatus.rejected
        st.approval.decided_by = ctx.actor.id
        st.approval.decided_at = ctx.now
        st.approval.decision_reason = args.note
        st.refund.status = RefundStatus.rejected
        st.refund.processed_at = ctx.now
        await session.flush()
        refund_service.emit(
            session,
            st.refund,
            st.customer_id,
            EventType.REFUND_REJECTED,
            request_id=ctx.request_id,
            rejected_by=ctx.actor.id,  # the admin's free-text note stays in the approvals row
        )
        audit.record(
            session,
            actor_id=ctx.actor.id,
            action="refund.rejected",
            resource_type="refund",
            resource_id=st.refund.id,
            decision=decision.policy.decision,
            reason_code=decision.policy.reason_code.value,
            reason=args.note,
            details={"approval_id": st.approval.id},
            request_id=ctx.request_id,
        )
        return ToolResult(
            data=response_data(
                st.refund, decision.policy, replayed=False, approval_id=st.approval.id
            ),
            http_status=200,
        )
