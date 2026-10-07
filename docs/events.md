# Events

How state changes leave the application: **transactional outbox -> relay -> Redpanda -> consumers**.
Request handlers never publish; they write an `outbox_events` row in the same database transaction as
the change (golden rule 7). Everything below is covered by tests, named in brackets.

```text
request ──(one DB transaction)──▶ refund / ticket row  +  outbox_events row
                                              │
              relay (python -m app.events.relay) polls unpublished rows
                                              ▼
                       Redpanda topics  {prefix}.refunds / {prefix}.tickets
                                              ▼
        consumers (python -m app.events.consumers.notifications) ──▶ effect
                       └── bad messages ──▶ {prefix}.dead-letter
```

## The envelope (version 1)

Every message value is this JSON object. The Kafka **key** is `aggregate_id`, so all events of one
refund (or ticket) land in one partition, in order. Message headers repeat `event_id`, `event_type`
and `version` for routing without parsing.

| Field | Meaning |
|---|---|
| `event_id` | UUID; the outbox row id. Stable across redeliveries: the key for de-duplication. |
| `event_type` | One of the types below. |
| `version` | Envelope/schema version (`1`). A consumer rejects versions it does not know. |
| `occurred_at` | ISO-8601 UTC; when the outbox row was written (the start of the originating transaction). |
| `request_id` | The HTTP request that caused it (traces a message back to the logs), or `null`. |
| `aggregate_type` | `refund` or `ticket`. Selects the topic: `{prefix}.refunds` / `{prefix}.tickets`. |
| `aggregate_id` | UUID of the refund or ticket. Also the Kafka key. |
| `data` | The event payload: **ids and state only** (below). |

### Ids and state only

Events carry no personal data and no free text: no names, emails, subjects, notes, addresses or
provider messages. `data` may only contain the fields listed here. This is enforced when the event is
**written** (`add_event` raises for any other field) and again when it is **read** (a consumer
dead-letters a message with unexpected fields). Adding a field is a deliberate schema change in
`app/events/types.py` (`EVENT_DATA_FIELDS`) plus this document.
[`test_add_event_refuses_personal_data_and_free_text_fields`,
`test_events_carry_ids_and_state_only_no_personal_data`]

All amounts are exact decimal strings (`"250.00"`) in `currency` (`INR`).

### Refund events (`aggregate_type = refund`, `aggregate_id = refund_id`)

Common fields of every refund event: `refund_id`, `order_id`, `customer_id` (the order's owner),
`requested_by` (who filed it; an admin may file on a customer's behalf), `amount`, `currency`,
`reason_code`, `status` (the refund's status when the event was written).

| `event_type` | Written when | Extra `data` fields |
|---|---|---|
| `refund.requested` | A refund is created (`processing` or `pending_approval`). | none |
| `refund.approved` | Policy auto-approved it (`auto: true`) or an admin approved it. | `auto` (bool), `approved_by` (admin user id, `null` when automatic) |
| `refund.completed` | The provider paid it out; status `succeeded`. | `provider_ref` (the provider's reference) |
| `refund.failed` | The provider declined it; status `failed`. | `failure_code` (a code such as `PAYOUT_DECLINED`, never the provider's text) |
| `refund.rejected` | An admin rejected it; status `rejected`. | `rejected_by` (admin user id) |

Typical sequences for one refund: `requested, approved, completed` (small refund);
`requested` ... later `approved, completed` (needed approval); `requested, rejected`;
`requested, approved, failed`. [`test_an_auto_approved_refund_travels_...`,
`test_the_approval_path_declined_payouts_rejections_and_tickets_all_flow`]

### Ticket events (`aggregate_type = ticket`, `aggregate_id = ticket_id`)

| `event_type` | Written when | `data` fields |
|---|---|---|
| `ticket.created` | A support ticket is created. | `ticket_id`, `user_id`, `order_id` (or `null`), `priority` |

The ticket's subject and body are deliberately **not** in the event; a consumer that needs them looks
the ticket up by id with its own authorisation.

## Delivery guarantees

* **At-least-once.** A row is marked `published_at` only **after** the broker acknowledged the
  message, in the same transaction that holds the row's lock. A crash between the acknowledgement and
  the commit publishes the event again on the next run. The producer uses `acks=all` and
  idempotence. [`test_a_relay_crash_between_the_ack_and_the_commit_republishes_but_never_loses`]
* **Effectively-once at the consumer.** The consumer records `(consumer, event_id)` in
  `processed_events` in the **same transaction as its effect**, and commits the Kafka offset only
  **after** that transaction commits. A redelivery finds the record and does nothing.
  [`test_a_crash_after_the_effect_but_before_the_offset_commit_has_exactly_one_effect`,
  `test_a_duplicate_delivery_does_not_notify_twice`] An effect *outside* the database (an email) can
  still be repeated if the process dies between performing it and committing; the notification stub
  only logs.
* **Ordering per aggregate.** The relay never publishes an event while an earlier unpublished one for
  the same aggregate exists, and the Kafka key keeps one aggregate in one partition. There is **no**
  ordering across aggregates. [`test_per_aggregate_order_is_kept_even_when_relays_overlap`]
* **Safe to run several relays.** Rows are claimed with `FOR UPDATE SKIP LOCKED`.
  [`test_two_relays_running_at_once_never_double_publish`,
  `test_a_second_relay_is_not_blocked_by_the_rows_the_first_has_locked`]

## Failure handling

### Relay: retry, backoff, parking

A failed publish (broker down, timeout) increments `attempts`, stores `last_error` and sets
`next_attempt_at` = now + `base * 2^(attempts so far)`, capped. The row is not retried before then.
After `OUTBOX_MAX_ATTEMPTS` failures the row is **parked** (`parked_at` set): the relay stops retrying
it and logs an **error** (`outbox_event_parked`).
[`test_an_unreachable_broker_leaves_the_row_unpublished_with_attempts_and_backoff`,
`test_after_max_attempts_the_event_is_parked_and_an_error_is_logged`]

| Setting | Default | Meaning |
|---|---|---|
| `OUTBOX_MAX_ATTEMPTS` | `10` | Failures before parking. |
| `OUTBOX_BACKOFF_BASE_S` | `1.0` | Delay after the first failure; doubles each time. |
| `OUTBOX_BACKOFF_MAX_S` | `300.0` | Cap on the delay. |
| `OUTBOX_BATCH_SIZE` | `50` | Rows claimed per cycle. |
| `OUTBOX_POLL_INTERVAL_S` | `1.0` | Sleep when nothing is due. |
| `KAFKA_TOPIC_PREFIX` | `ops` | Topic prefix (`ops.refunds`, `ops.tickets`, `ops.dead-letter`). |

With the defaults the waits are 1, 2, 4, ... 256 s (511 s in total) before the 10th and last attempt, so a
broker outage of about nine minutes parks the events that were waiting. That is a deliberate
trade-off, not an oversight; see below.

### A parked event blocks its aggregate (trade-off)

A parked event is still *unpublished*, so by the ordering rule it **blocks every later event of the
same aggregate** until an operator resolves it. This favours correct order and completeness for one
refund over availability for that refund; other aggregates are unaffected.
[`test_a_parked_event_blocks_later_events_of_its_aggregate_until_unparked`,
`test_one_failing_event_does_not_block_other_aggregates`] The same applies to an event waiting in
backoff. Operate it with:

```text
python scripts/outbox_admin.py list --state parked      # what is parked
python scripts/outbox_admin.py blocked                  # how many events each one blocks
python scripts/outbox_admin.py unpark --aggregate <id> [--dry-run]    # or --id <uuid>, or --all
```

`unpark` clears `parked_at`, resets `attempts`, and makes the event due immediately. Fix the cause
first (broker down, bad data), then un-park; alert on the `outbox_event_parked` error log.

### Consumer: poison messages and dead-lettering

A message that can never succeed is **dead-lettered**, not retried: it is copied (original bytes) to
`{prefix}.dead-letter` with headers `x-dlq-reason`, `x-dlq-detail`, `x-original-topic`,
`x-original-partition`, `x-original-offset`, `x-consumer`, then skipped so the partition keeps moving.

| `x-dlq-reason` | Cause |
|---|---|
| `empty_message` | Empty value. |
| `invalid_json` | Not JSON / not UTF-8. |
| `not_an_object` | JSON, but not an object. |
| `invalid_envelope` | Missing or wrongly typed envelope fields, or extra top-level fields. |
| `unsupported_version` | `version` other than `1`. |
| `unknown_event_type` | `event_type` not in the table above. |
| `unexpected_fields` | `data` contains fields outside the allow-list. |
| `handler_failed` | The effect kept failing (`CONSUMER_MAX_HANDLER_ATTEMPTS`, default 5, with backoff). |

[`test_poison_messages_are_dead_lettered_without_crashing_or_blocking`,
`test_a_handler_that_always_fails_is_dead_lettered_and_does_not_block_the_partition`]

Infrastructure trouble (database or broker down) is different: the consumer does **not** commit the
offset, rewinds to that message and retries after a pause, so nothing is lost.

## Running the processes

```text
python -m app.events.relay [--once] [--batch-size N] [--poll-interval S]
python -m app.events.consumers.notifications [--once] [--idle-polls N]
```

Both are separate processes, finish their current unit of work on `SIGINT`/`SIGTERM`, then exit
cleanly. `--once` drains what is due (relay) or the backlog (consumer) and exits; the test suite uses
it. [`test_the_cli_entry_points_work_as_real_processes`]
