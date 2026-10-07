"""Loading and validating rules.yaml. Fails fast and loudly: a bad rules file must stop the app
from starting, never silently weaken a rule."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core.config import get_settings
from app.policy.types import OrderStatus

DEFAULT_RULES_PATH = Path(__file__).with_name("rules.yaml")


class RulesError(Exception):
    """rules.yaml is missing, malformed, or fails validation."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _reject_float_money(v: Any) -> Any:
    if isinstance(v, float):
        raise ValueError('amounts must be quoted strings (e.g. "500.00") or integers, not floats')
    return v


class RefundRules(_Frozen):
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    auto_approve_max: Decimal
    max_order_age_days: Annotated[int, Field(gt=0)]
    refundable_statuses: Annotated[frozenset[OrderStatus], Field(min_length=1)]
    velocity_window_days: Annotated[int, Field(gt=0)]
    velocity_max_refunds: Annotated[int, Field(ge=0)]
    valid_reasons: Annotated[frozenset[str], Field(min_length=1)]
    auto_approve_reasons: frozenset[str]

    _money = field_validator("auto_approve_max", mode="before")(_reject_float_money)

    @field_validator("auto_approve_max")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("auto_approve_max must be positive")
        return v

    @model_validator(mode="after")
    def _auto_reasons_are_valid_reasons(self) -> "RefundRules":
        unknown = self.auto_approve_reasons - self.valid_reasons
        if unknown:
            raise ValueError(f"auto_approve_reasons not in valid_reasons: {sorted(unknown)}")
        return self


class CancelRules(_Frozen):
    cancellable_statuses: frozenset[OrderStatus]
    refund_required_statuses: frozenset[OrderStatus]

    @model_validator(mode="after")
    def _disjoint(self) -> "CancelRules":
        both = self.cancellable_statuses & self.refund_required_statuses
        if both:
            raise ValueError(
                f"a status cannot be both cancellable and refund-required: {sorted(both)}"
            )
        return self


class Rules(_Frozen):
    version: Annotated[int, Field(ge=1)]
    refund: RefundRules
    cancel: CancelRules
    admin_only_actions: frozenset[str]
    separation_of_duties_actions: frozenset[str]


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML silently keeps the last one,
    which would let a second `auto_approve_max:` quietly override the first)."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def parse_rules(text: str, source: str = "<string>") -> Rules:
    try:
        raw = yaml.load(text, Loader=_StrictLoader)  # noqa: S506  (safe subclass, no objects)
    except yaml.YAMLError as exc:
        raise RulesError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RulesError(f"{source}: top level must be a mapping")
    try:
        return Rules.model_validate(raw)
    except ValidationError as exc:
        raise RulesError(f"{source}: {exc}") from exc


def load_rules(path: Path | str = DEFAULT_RULES_PATH) -> Rules:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesError(f"cannot read rules file {p}: {exc}") from exc
    return parse_rules(text, source=str(p))


@lru_cache
def get_rules() -> Rules:
    """The process-wide rules, loaded once from POLICY_RULES_PATH (default: the packaged file)."""
    configured = get_settings().policy_rules_path
    return load_rules(configured or DEFAULT_RULES_PATH)
