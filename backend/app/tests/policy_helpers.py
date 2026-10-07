"""Builders for policy tests: a baseline rule set and baseline facts, with overrides."""

import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import yaml

from app.policy import Actor, RefundFacts, Rules
from app.policy.rules import DEFAULT_RULES_PATH
from app.policy.types import OrderStatus, Role

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)

_BASE_RULES: dict[str, Any] = yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))


def make_rules(**overrides: Any) -> Rules:
    """The real rules.yaml with dotted-path overrides.

    Example: make_rules(**{"refund.auto_approve_max": "1000.00"}).
    """
    raw = deepcopy(_BASE_RULES)
    for dotted, value in overrides.items():
        node = raw
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return Rules.model_validate(raw)


def make_facts(**overrides: Any) -> RefundFacts:
    base: dict[str, Any] = {
        "amount": Decimal("100.00"),
        "reason_code": "damaged",
        "order_status": OrderStatus.delivered,
        "order_total": Decimal("1000.00"),
        "order_currency": "INR",
        "order_placed_at": NOW - 5 * DAY,
        "has_succeeded_payment": True,
        "already_refunded": Decimal("0.00"),
        "recent_refund_count": 0,
    }
    base.update(overrides)
    return RefundFacts(**base)


def actor(role: Role = Role.customer, user_id: uuid.UUID | None = None) -> Actor:
    return Actor(user_id=user_id or uuid.uuid4(), role=role)
