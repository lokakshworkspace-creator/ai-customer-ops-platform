"""rules.yaml loading: fail-fast validation (a bad file must never weaken a rule silently)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.policy.rules as rules_module
from app.policy import RulesError, load_rules, parse_rules
from app.policy.rules import DEFAULT_RULES_PATH
from app.policy.types import OrderStatus

GOOD = DEFAULT_RULES_PATH.read_text(encoding="utf-8")


def test_the_shipped_rules_file_loads() -> None:
    rules = load_rules()
    assert rules.version == 1
    assert rules.refund.auto_approve_max == Decimal("500.00")
    assert rules.refund.refundable_statuses == {OrderStatus.paid, OrderStatus.delivered}
    assert rules.refund.auto_approve_reasons <= rules.refund.valid_reasons
    assert rules.admin_only_actions == {"approve_refund", "reject_refund"}


def test_rules_are_immutable() -> None:
    rules = load_rules()
    with pytest.raises(Exception):  # noqa: B017  (pydantic ValidationError on frozen model)
        rules.refund.auto_approve_max = Decimal("999999")


def test_amounts_may_be_integers_or_quoted_strings() -> None:
    assert parse_rules(GOOD.replace('"500.00"', "500")).refund.auto_approve_max == Decimal(500)
    assert parse_rules(GOOD.replace('"500.00"', '"499.50"')).refund.auto_approve_max == Decimal(
        "499.50"
    )


@pytest.mark.parametrize(
    ("find", "replace", "message"),
    [
        ('"500.00"', "500.5", "not floats"),  # an unquoted float
        ('"500.00"', '"-1"', "positive"),
        ('"500.00"', '"abc"', "auto_approve_max"),
        ("max_order_age_days: 30", "max_order_age_days: 0", "greater than 0"),
        ("velocity_max_refunds: 3 ", "velocity_max_refunds: -1 ", "greater than or equal to 0"),
        (
            "refundable_statuses: [paid, delivered]",
            "refundable_statuses: [paid, teleported]",
            "teleported",
        ),
        ("refundable_statuses: [paid, delivered]", "refundable_statuses: []", "at least 1"),
        ("  currency: INR ", "  currency: rupees ", "pattern"),
        (
            "    - late_delivery\n\ncancel",
            "    - late_delivery\n    - vibes\n\ncancel",
            "auto_approve_reasons",
        ),
        (
            "refund_required_statuses: [paid]",
            "refund_required_statuses: [pending]",
            "both cancellable",
        ),
        ("version: 1", "version: 0", "greater than or equal to 1"),
        (
            "version: 1\n",
            "version: 1\nsurprise_rule: true\n",
            "surprise_rule",
        ),  # unknown top-level key
        ("  currency: INR", "  currency: INR\n  typo_limit: 5", "typo_limit"),  # unknown nested key
    ],
)
def test_invalid_rules_are_rejected(find: str, replace: str, message: str) -> None:
    assert find in GOOD, f"test setup: {find!r} not in rules.yaml"
    with pytest.raises(RulesError, match=message):
        parse_rules(GOOD.replace(find, replace, 1))


def test_missing_required_section_is_rejected() -> None:
    without_cancel = (
        GOOD.split("cancel:")[0] + "admin_only_actions: []\nseparation_of_duties_actions: []\n"
    )
    with pytest.raises(RulesError, match="cancel"):
        parse_rules(without_cancel)


def test_duplicate_keys_are_rejected_instead_of_silently_last_wins() -> None:
    dup = GOOD.replace(
        'auto_approve_max: "500.00"', 'auto_approve_max: "500.00"\n  auto_approve_max: "99999.00"'
    )
    with pytest.raises(RulesError, match="duplicate key"):
        parse_rules(dup)


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string", "{unclosed"])
def test_non_mapping_or_broken_yaml_is_rejected(text: str) -> None:
    with pytest.raises(RulesError):
        parse_rules(text)


def test_missing_file_is_a_rules_error(tmp_path) -> None:
    with pytest.raises(RulesError, match="cannot read"):
        load_rules(tmp_path / "nope.yaml")


def test_get_rules_uses_the_configured_path(tmp_path, monkeypatch) -> None:
    custom = tmp_path / "custom.yaml"
    custom.write_text(GOOD.replace('"500.00"', '"750.00"'), encoding="utf-8")
    rules_module.get_rules.cache_clear()
    try:
        monkeypatch.setattr(
            rules_module, "get_settings", lambda: SimpleNamespace(policy_rules_path=str(custom))
        )
        assert rules_module.get_rules().refund.auto_approve_max == Decimal("750.00")

        monkeypatch.undo()  # back to the real settings: the packaged default applies
        rules_module.get_rules.cache_clear()
        assert rules_module.get_rules().refund.auto_approve_max == Decimal("500.00")
    finally:
        rules_module.get_rules.cache_clear()
