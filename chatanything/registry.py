"""Capability registry: the mechanical heart of "everything is a plugin".

Plugins contribute components into named capability slots.  The runtime never
imports a concrete implementation directly; it resolves the current default
for a slot.  Replacing a component therefore means registering a plugin with a
higher priority -- no core changes required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Iterable, Mapping, TypeVar, cast


class Capability(str, Enum):
    """Well-known extension slots."""

    LLM = "llm"
    FILTER = "filter"
    CONTEXT = "context"
    MEMORY = "memory"
    ROUTER_POLICY = "router_policy"
    PROMPT = "prompt"
    PARSER = "parser"
    AGENT_LOOP = "agent_loop"
    CHANNEL = "channel"
    TOOL = "tool"
    WATCHDOG = "watchdog"
    ENVIRONMENT = "environment"
    SPECULATOR = "speculator"
    EVENT_SOURCE = "event_source"
    CONFIG_STORE = "config_store"
    MCP = "mcp"
    SKILL = "skill"
    STREAM = "stream"


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ComponentEntry(Generic[T]):
    """A registered component plus its provenance metadata."""

    slot: Capability
    name: str
    component: T
    plugin: str = ""
    priority: int = 0
    order: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class ComponentRegistry:
    """A small, typed service locator."""

    def __init__(self) -> None:
        self._entries: dict[Capability, dict[str, ComponentEntry[Any]]] = {}
        self._defaults: dict[Capability, str] = {}
        self._order = 0
        self._default_overrides: dict[Capability, str] = {}

    def register(
        self,
        slot: Capability,
        name: str,
        component: Any,
        *,
        plugin: str = "",
        priority: int = 0,
        default: bool = True,
        replace: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ComponentEntry[Any]:
        """Register a component and optionally make it the slot default."""

        from .errors import DuplicateComponentError

        bucket = self._entries.setdefault(slot, {})
        if name in bucket and not replace:
            raise DuplicateComponentError(slot.value, name)
        self._order += 1
        entry = ComponentEntry(
            slot=slot,
            name=name,
            component=component,
            plugin=plugin,
            priority=priority,
            order=self._order,
            metadata=metadata or {},
        )
        bucket[name] = entry
        if default and slot not in self._default_overrides:
            self._defaults[slot] = name
        return entry

    def set_default(self, slot: Capability, name: str) -> None:
        """Explicitly select the default implementation for a slot."""

        from .errors import ComponentNotFoundError

        if name not in self._entries.get(slot, {}):
            raise ComponentNotFoundError(slot.value, name)
        self._defaults[slot] = name
        self._default_overrides[slot] = name

    def default_name(self, slot: Capability) -> str | None:
        return self._defaults.get(slot)

    def resolve(self, slot: Capability, name: str | None = None) -> Any:
        """Resolve a named component or the slot default."""

        from .errors import ComponentNotFoundError

        bucket = self._entries.get(slot, {})
        target = name or self._defaults.get(slot)
        if target is None or target not in bucket:
            raise ComponentNotFoundError(slot.value, name)
        return bucket[target].component

    def resolve_entry(self, slot: Capability, name: str | None = None) -> ComponentEntry[Any]:
        from .errors import ComponentNotFoundError

        bucket = self._entries.get(slot, {})
        target = name or self._defaults.get(slot)
        if target is None or target not in bucket:
            raise ComponentNotFoundError(slot.value, name)
        return bucket[target]

    def resolve_all(self, slot: Capability) -> tuple[Any, ...]:
        """All registered components of a slot, ordered by priority then install order."""

        bucket = self._entries.get(slot, {})
        ordered = sorted(bucket.values(), key=lambda entry: (-entry.priority, entry.order))
        return tuple(entry.component for entry in ordered)

    def entries(self, slot: Capability) -> tuple[ComponentEntry[Any], ...]:
        bucket = self._entries.get(slot, {})
        return tuple(sorted(bucket.values(), key=lambda entry: entry.order))

    def names(self, slot: Capability) -> tuple[str, ...]:
        return tuple(self._entries.get(slot, {}).keys())

    def contains(self, slot: Capability, name: str) -> bool:
        return name in self._entries.get(slot, {})

    def remove(self, slot: Capability, name: str) -> None:
        bucket = self._entries.get(slot, {})
        bucket.pop(name, None)
        if self._defaults.get(slot) == name:
            self._defaults.pop(slot, None)
            if bucket:
                fallback = sorted(bucket.values(), key=lambda item: (-item.priority, item.order))[0]
                self._defaults[slot] = fallback.name
        if self._default_overrides.get(slot) == name:
            self._default_overrides.pop(slot, None)
        if not bucket:
            self._entries.pop(slot, None)

    def unregister_plugin(self, plugin: str) -> int:
        """Remove every component contributed by a plugin."""

        removed = 0
        for slot, bucket in list(self._entries.items()):
            for name, entry in list(bucket.items()):
                if entry.plugin == plugin:
                    self.remove(slot, name)
                    removed += 1
        self._default_overrides = {
            slot: name for slot, name in self._default_overrides.items() if self.contains(slot, name)
        }
        return removed

    def typed(self, slot: Capability, expected: type[T], name: str | None = None) -> T:
        """Resolve a component with a static return type (runtime checked by caller)."""

        return cast(T, self.resolve(slot, name))

    def snapshot(self) -> dict[str, tuple[str, ...]]:
        """A stable, log-friendly view of every registered capability."""

        return {slot.value: tuple(bucket.keys()) for slot, bucket in self._entries.items()}

    # ------------------------------------------------------------------
    # Snapshot / restore (used by hot-plug transactions)
    # ------------------------------------------------------------------
    def snapshot_state(self) -> dict[str, Any]:
        """Capture the whole registry so a failed hot-plug operation can roll back."""

        return {
            "entries": {
                slot: dict(bucket) for slot, bucket in self._entries.items()
            },
            "defaults": dict(self._defaults),
            "overrides": dict(self._default_overrides),
            "order": self._order,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        """Replace the registry contents with a previously captured snapshot."""

        self._entries = {
            slot: dict(bucket)
            for slot, bucket in state["entries"].items()  # type: ignore[union-attr]
        }
        self._defaults = dict(state["defaults"])  # type: ignore[arg-type]
        self._default_overrides = dict(state["overrides"])  # type: ignore[arg-type]
        self._order = int(state["order"])

    def __iter__(self) -> Iterable[Capability]:
        return iter(self._entries)
