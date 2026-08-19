"""
Deterministic tool registry (Phase 4; plan.md Step 4.3).

A tool becomes callable through ToolOrchestrator only by being explicitly
registered here at construction time. There is no mechanism anywhere in
this module — or in tool_orchestrator.py — for a tool to be registered
dynamically based on model output; the model can never cause a new name
to become resolvable. Lookup is pure (never executes the registered
callable), deterministic, and duplicate/invalid registrations are
rejected at registration time, not silently overwritten.
"""

from typing import Callable, Optional

from action_models import ActionSpec


class ToolRegistrationError(ValueError):
    """Raised when register() is given an invalid or duplicate tool definition."""


class ToolRegistry:
    def __init__(self):
        self._specs: dict[str, ActionSpec] = {}
        self._callables: dict[str, Callable[[dict], dict]] = {}

    def register(self, spec: ActionSpec, fn: Callable[[dict], dict]) -> None:
        if not isinstance(spec, ActionSpec):
            raise ToolRegistrationError("spec must be an ActionSpec instance")
        if not isinstance(spec.name, str) or not spec.name.strip():
            raise ToolRegistrationError("ActionSpec.name must be a non-empty string")
        if spec.name in self._specs:
            raise ToolRegistrationError(f"Tool '{spec.name}' is already registered — duplicate registration rejected")
        if not callable(fn):
            raise ToolRegistrationError(f"Implementation for tool '{spec.name}' must be callable")
        self._specs[spec.name] = spec
        self._callables[spec.name] = fn

    def is_registered(self, name) -> bool:
        return isinstance(name, str) and name in self._specs

    def get_spec(self, name) -> Optional[ActionSpec]:
        if not isinstance(name, str):
            return None
        return self._specs.get(name)

    def get_callable(self, name) -> Optional[Callable[[dict], dict]]:
        """
        Returns the registered implementation for lookup/inspection
        purposes (e.g. so ToolOrchestrator can call it after all policy
        checks pass). Retrieving the callable does not invoke it — only
        ToolOrchestrator._execute() ever calls it, and only after every
        prior gate has already passed.
        """
        if not isinstance(name, str):
            return None
        return self._callables.get(name)

    def list_actions(self) -> list[ActionSpec]:
        return list(self._specs.values())
