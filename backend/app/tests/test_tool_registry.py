"""Registry invariants. No database needed."""

import pytest

from app.db.models.enums import Role
from app.tests.policy_helpers import make_rules
from app.tools.default import build_default_registry
from app.tools.handlers.base import Decision, ToolResult
from app.tools.registry import ToolRegistry, ToolSpec
from app.tools.schemas import GetOrderInput, ToolInput


class _Noop:
    async def decide(self, session, ctx, args, rules) -> Decision:  # pragma: no cover
        raise NotImplementedError

    async def execute(self, session, ctx, args, decision) -> ToolResult:  # pragma: no cover
        raise NotImplementedError


def spec(
    name="do_thing", roles=frozenset({Role.customer}), visible=True, mutating=False
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="x",
        input_model=GetOrderInput,
        required_roles=frozenset(roles),
        mutating=mutating,
        agent_visible=visible,
        handler=_Noop(),
    )


def test_default_registry_has_the_expected_tools() -> None:
    reg = build_default_registry(make_rules())
    by_name = {t.name: t for t in reg.all()}
    assert {"get_order", "list_orders", "create_ticket", "cancel_order"} <= set(by_name)
    assert not by_name["get_order"].mutating and not by_name["list_orders"].mutating
    assert by_name["create_ticket"].mutating and by_name["cancel_order"].mutating


def test_every_tool_declares_roles_and_forbids_unknown_arguments() -> None:
    for t in build_default_registry(make_rules()).all():
        assert t.required_roles, t.name
        assert t.input_model.model_config.get("extra") == "forbid", t.name
        assert t.input_schema()["additionalProperties"] is False, t.name
        assert "user_id" not in t.input_schema()["properties"], t.name


def test_no_tool_takes_a_caller_identity_argument() -> None:
    forbidden = {"user_id", "actor_id", "requested_by", "customer_id", "role", "idempotency_key"}
    for t in build_default_registry(make_rules()).all():
        assert not forbidden & set(t.input_schema()["properties"]), t.name


def test_registration_rejects_duplicates_bad_names_and_empty_roles() -> None:
    reg = ToolRegistry(make_rules())
    reg.register(spec("ok_tool"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(spec("ok_tool"))
    for bad in ("", "X", "Has-Dash", "9starts_with_digit", "a" * 70, "has space"):
        with pytest.raises(ValueError, match="invalid tool name"):
            reg.register(spec(bad))
    with pytest.raises(ValueError, match="at least one required role"):
        reg.register(spec("no_roles", roles=frozenset()))


@pytest.mark.parametrize(
    "name_roles",
    [
        ("approve_refund", {Role.admin}),  # admin-only by name (rules.yaml) and by role
        ("reject_refund", {Role.customer, Role.admin}),  # admin-only by name even if roles are wide
        ("made_up_admin_tool", {Role.admin}),  # admin-only by role alone
    ],
)
def test_admin_only_tools_can_never_be_agent_visible(name_roles) -> None:
    name, roles = name_roles
    reg = ToolRegistry(make_rules())
    with pytest.raises(ValueError, match="must not be agent_visible"):
        reg.register(spec(name, roles=roles, visible=True))
    reg.register(spec(name, roles=roles, visible=False))  # hidden is fine
    assert reg.agent_tools() == []
    assert [t.name for t in reg.all()] == [name]


def test_admin_only_list_in_rules_is_honoured_not_hardcoded() -> None:
    reg = ToolRegistry(make_rules(admin_only_actions=["close_account"]))
    with pytest.raises(ValueError, match="must not be agent_visible"):
        reg.register(spec("close_account", roles={Role.customer, Role.admin}, visible=True))
    reg.register(spec("approve_refund", roles={Role.customer}, visible=True))  # no longer listed


def test_agent_tools_contains_only_visible_tools() -> None:
    reg = ToolRegistry(make_rules())
    reg.register(spec("visible_tool", visible=True))
    reg.register(spec("hidden_tool", visible=False))
    assert [t.name for t in reg.agent_tools()] == ["visible_tool"]
    assert {t.name for t in reg.all()} == {"visible_tool", "hidden_tool"}


def test_invariant_holds_for_the_default_registry() -> None:
    """Whatever gets added to the default registry later, admin tools stay hidden from the agent."""
    rules = make_rules()
    reg = build_default_registry(rules)
    visible = {t.name for t in reg.agent_tools()}
    for t in reg.all():
        if t.required_roles == frozenset({Role.admin}) or t.name in rules.admin_only_actions:
            assert t.name not in visible, f"{t.name} is admin-only but agent-visible"
    # and the converse: every rules.yaml admin-only action that IS registered requires admin
    for t in reg.all():
        if t.name in rules.admin_only_actions:
            assert t.required_roles == frozenset({Role.admin}), t.name


def test_get_returns_none_for_unknown_tools() -> None:
    assert build_default_registry(make_rules()).get("rm_rf") is None


def test_tool_input_base_is_frozen_and_forbids_extras() -> None:
    assert ToolInput.model_config["extra"] == "forbid" and ToolInput.model_config["frozen"]
