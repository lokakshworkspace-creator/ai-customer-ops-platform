import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.db.models.enums import Role
from app.policy.rules import Rules
from app.tools.handlers.base import ToolHandler

_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    required_roles: frozenset[Role]
    mutating: bool  # False = read-only
    agent_visible: bool  # may the (Phase 3) LLM agent propose this tool?
    handler: ToolHandler

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()


class ToolRegistry:
    def __init__(self, rules: Rules) -> None:
        self._rules = rules
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not _NAME.match(spec.name):
            raise ValueError(f"invalid tool name {spec.name!r}")
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        if not spec.required_roles:
            raise ValueError(f"tool {spec.name!r} must declare at least one required role")
        # An administrator-only capability must never be offered to the LLM agent, whatever
        # the tool author wrote: derived from both the declared roles and rules.yaml.
        admin_only = (
            spec.required_roles == frozenset({Role.admin})
            or spec.name in self._rules.admin_only_actions
        )
        if admin_only and spec.agent_visible:
            raise ValueError(f"admin-only tool {spec.name!r} must not be agent_visible")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def agent_tools(self) -> list[ToolSpec]:
        """The only tools an LLM agent may be shown or may propose."""
        return [t for t in self._tools.values() if t.agent_visible]
