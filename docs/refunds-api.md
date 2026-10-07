# Refunds API

Refund requests, approvals and payouts (Phase 2). Everything here is backed by a test; the last
column of each table names it. The interactive version of the request table is also served by the
running API at `/docs`.

All endpoints require `Authorization: Bearer <token>` (see the README for logging in).

## `POST /api/v1/refunds`

Request a refund for one of **your** orders (admins may file one for any order).

**Headers**

| Header | Required | Rules |
|---|---|---|
| `Idempotency-Key` | **yes** | 8-255 characters of `A-Z a-z 0-9 . _ : -`. Unique **per user**. Missing or invalid: `422`. |

**Body** (JSON; unknown fields are rejected with `422`)

| Field | Type | Rules |
|---|---|---|
| `order_id` | uuid | Must be your order, otherwise `404` (identical to a missing order). |
| `amount` | decimal **string** or integer | `"250.00"`, `250`. Greater than 0, at most 2 decimal places, at most `9999999999.99`. A JSON float (`250.5`) is rejected: money is never a float. |
| `reason_code` | string | `^[a-z][a-z_]{0,31}$`. Which reasons exist, and which are auto-approvable, is configuration (`rules.yaml`). |
| `note` | string, optional | 1-500 characters. |

There is no user, key or role field: the caller is the authenticated user, and the key is the
header.

### Responses

| Situation | HTTP | Body highlights | Test |
|---|---|---|---|
| Within policy: paid out immediately | **201** | `refund.status = "succeeded"`, `replayed: false` | `test_small_refund_is_paid_immediately` |
| Exactly at the automatic limit (500.00) | **201** | as above | `test_the_automatic_limit_boundary` |
| Needs a human (amount over the limit, the customer's 4th refund in 30 days, a reason that needs review) | **202** | `refund.status = "pending_approval"`, `approval_id`, `policy.reason_code` | `test_large_refund_waits_for_approval_...`, `test_the_fourth_refund_in_30_days_needs_approval` |
| **Provider declined the payout** | **201** | `refund.status = "failed"` and an `error` object `{code: "PAYOUT_FAILED", message}` | `test_a_declined_payout_is_a_201_with_status_failed_...` |
| Provider did not answer (outcome unknown) | **202** | `refund.status = "processing"`, `error.code = "PAYOUT_UNCONFIRMED"`; retried by the reconcile script | `test_an_unconfirmed_payout_is_202_processing_...` |
| Same `Idempotency-Key` + same payload again | **200** | the original refund (current state), `replayed: true`, header `Idempotent-Replayed: true` | `test_same_key_ten_times_sequentially_makes_one_refund` |
| Same `Idempotency-Key`, **different** payload | **409** | `code: "IDEMPOTENCY_KEY_REUSED"` | `test_same_key_different_payload_is_409` |
| Forbidden by policy (order too old, not refundable, more than the order total or what remains, unknown reason) | **403** | `code` (e.g. `ORDER_TOO_OLD`, `AMOUNT_EXCEEDS_ORDER`), `decision: "DENY"`, human `detail`; **nothing is created** | `test_a_50000_rupee_refund_is_denied_...`, `test_policy_denials` |
| Order missing **or not yours** | **404** | `detail: "Order not found"` (identical for both) | `test_another_customers_order_is_404_...` |
| Not logged in / bad token | **401** | | `test_unauthenticated_requests_get_401` |
| Invalid body or header | **422** | | `test_invalid_bodies_are_rejected_...`, `test_idempotency_key_is_required_and_validated` |

### A declined payout is a 201, on purpose

The request was valid and accepted, and the refund is recorded, so the response is **201 Created**
with `refund.status = "failed"` plus an `error` object. It is not an HTTP error: the refund exists
and can be fetched.

`failed` is **terminal for that Idempotency-Key**. Repeating the same key returns the same failed
refund (`200`, `replayed: true`) and does **not** contact the provider again. To try again, send a
**new** Idempotency-Key. A failed refund releases its amount, so it does not count against the
order's refundable total or the customer's refund-frequency limit.

(Test: `test_a_declined_payout_is_a_201_with_status_failed_and_is_terminal_for_the_key`.)

### How replays and races behave

* Keys are scoped to the requesting user; two users may use the same key and get separate refunds
  and separate provider payouts (`test_keys_are_scoped_per_user_...`).
* Ten identical requests sent at once produce exactly one refund and one provider payout: one
  `201`, nine `200` replays (`test_ten_concurrent_identical_requests_...`).
* The payload comparison is canonical: `"500"`, `"500.0"` and `"500.00"` are the same amount
  (`test_equivalent_amount_spellings_are_the_same_request`).
* Replays return the refund's **current** state, which may have advanced since the first response
  (for example `pending_approval` to `succeeded`).

## `GET /api/v1/refunds/{refund_id}`

`200` with `{"refund": {...}}` for a refund on your own order (admins: any); `404` otherwise
(`test_get_refund_is_scoped_to_the_order_owner`).

## Approvals (administrators)

| Method and path | Purpose | Notes |
|---|---|---|
| `GET /api/v1/approvals?status=pending&limit=20&offset=0` | List approvals | Admin only (`403` for customers). `limit` 1-100. |
| `POST /api/v1/approvals/{approval_id}/approve` | Approve and pay out | Optional body `{"note": "..."}`. `200` with the refund (which may be `failed` or, if unconfirmed, `202 processing`). |
| `POST /api/v1/approvals/{approval_id}/reject` | Reject | Optional body `{"note": "..."}`. `200`, refund becomes `rejected`, nothing is paid. |

| Situation | HTTP | Test |
|---|---|---|
| Already decided (also when two admins act at once) | **409** `ALREADY_DECIDED` | `test_an_approval_can_only_be_decided_once`, `test_two_admins_approving_at_once_decide_exactly_once` |
| A customer calls it | **403** `ROLE_NOT_PERMITTED` (logged) | `test_a_customer_cannot_decide_and_the_attempt_is_logged` |
| An admin approves **their own** request | **403** `SELF_APPROVAL` | `test_an_admin_cannot_approve_their_own_request` |
| Approving after the order aged past 30 days | **403** `ORDER_TOO_OLD`; the approval stays open | `test_approval_is_denied_if_the_order_has_since_aged_out_of_the_window` |
| Unknown approval | **404** | `test_unknown_and_malformed_approvals` |

An admin may **reject** their own request (it is a withdrawal); which actions need a different
decider is configuration (`separation_of_duties_actions` in `rules.yaml`).

## Refund states

```
request ──ALLOW──────────────────▶ processing ──provider paid──▶ succeeded
   │                                  ▲   └─────provider declined─▶ failed   (terminal for the key)
   └─REQUIRE_APPROVAL─▶ pending_approval ──admin approves──┘
                              └──admin rejects──▶ rejected
(a DENY creates no refund at all)
```

`processing` that lasts more than a few minutes means the process died or the provider never
answered; `scripts/reconcile_refunds.py` retries those safely (see its `--help`).

## Events

Each state change writes an outbox row **in the same transaction**: `refund.requested`,
`refund.approved` (`auto: true` when policy approved it), `refund.completed`, `refund.failed`,
`refund.rejected` (and `ticket.created`). Publishing them to Redpanda is the next commit.
