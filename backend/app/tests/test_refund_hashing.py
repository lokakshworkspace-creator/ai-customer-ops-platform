"""Idempotency key validation and the canonical request hash. Pure, no database."""

import uuid
from decimal import Decimal

import pytest

from app.modules.refunds.hashing import (
    canonical_amount,
    is_valid_idempotency_key,
    request_hash,
)

ORDER = uuid.UUID("11111111-1111-1111-1111-111111111111")
BASE = {"order_id": ORDER, "amount": Decimal("100.00"), "reason_code": "damaged", "note": None}


def h(**overrides) -> str:
    return request_hash(**{**BASE, **overrides})


# --- canonicalisation: equivalent requests hash the same ---------------------------------


@pytest.mark.parametrize("variant", ["500", "500.0", "500.00", "5E+2", "0500", "500.000"])
def test_equivalent_decimals_hash_identically(variant: str) -> None:
    assert h(amount=Decimal(variant)) == h(amount=Decimal("500"))


@pytest.mark.parametrize(
    ("a", "b"),
    [("0.5", "0.50"), ("12.30", "12.3"), ("1", "1.00"), ("9999999999.99", "9999999999.990")],
)
def test_more_equivalent_amounts(a: str, b: str) -> None:
    assert h(amount=Decimal(a)) == h(amount=Decimal(b))


def test_canonical_amount_is_a_plain_non_scientific_string() -> None:
    assert canonical_amount(Decimal("5E+2")) == "500"
    assert canonical_amount(Decimal("0.10")) == "0.1"
    assert "E" not in canonical_amount(Decimal("1E+9"))


def test_blank_and_whitespace_notes_equal_no_note() -> None:
    assert h(note=None) == h(note="") == h(note="   ")
    assert h(note="  hello ") == h(note="hello")


def test_hash_is_deterministic_and_a_sha256_hex_digest() -> None:
    assert h() == h()
    assert len(h()) == 64 and set(h()) <= set("0123456789abcdef")


# --- every payload field matters ----------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("order_id", uuid.UUID("22222222-2222-2222-2222-222222222222")),
        ("amount", Decimal("100.01")),
        ("reason_code", "defective"),
        ("note", "please hurry"),
    ],
)
def test_changing_any_field_changes_the_hash(field: str, other) -> None:
    assert h(**{field: other}) != h()


def test_a_one_paisa_difference_is_a_different_request() -> None:
    assert h(amount=Decimal("500.00")) != h(amount=Decimal("500.01"))


def test_hash_covers_every_field_of_the_request_schema() -> None:
    """If someone adds a field to the refund request, the hash must be taught about it."""
    from app.tools.schemas import RequestRefundInput

    assert set(RequestRefundInput.model_fields) == set(BASE)


def test_field_boundaries_cannot_be_forged_by_concatenation() -> None:
    # JSON-encoded canonical form: values cannot bleed into neighbouring fields
    assert h(reason_code="damaged", note="x") != h(reason_code="damage", note="dx")


# --- Idempotency-Key validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "abcdefgh",
        "a" * 255,
        "550e8400-e29b-41d4-a716-446655440000",
        "req_2026.10.07:42-a",
        "A1b2C3d4",
    ],
)
def test_valid_keys(key: str) -> None:
    assert is_valid_idempotency_key(key)


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "short",
        "a" * 7,
        "a" * 256,  # missing / too short / too long
        "has space in it",
        "semi;colon-key",
        "slash/inside-key",
        "quote'key-here",
        "ünïcode-key-123",
        "abcdefgh\n",
        "abcdefgh\r\n",
        "abc\ndefghij",
        "abcdefgh\x00",
        "<script>alert1</script>",
    ],
)
def test_invalid_keys(key) -> None:
    assert not is_valid_idempotency_key(key)
