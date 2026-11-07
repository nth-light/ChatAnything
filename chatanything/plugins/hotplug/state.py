"""Plugin runtime states, metrics and monitoring snapshots."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PluginState(str, Enum):
    """Lifecycle state of one managed plugin."""

    REGISTERED = "registered"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    DISABLED = "disabled"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    FAILED = "failed"


_ACTIVE_STATES = frozenset(
    {
        PluginState.REGISTERED,
        PluginState.INITIALIZING,
        PluginState.INITIALIZED,
        PluginState.STARTING,
        PluginState.RUNNING,
        PluginState.STOPPING,
    }
)


@dataclass(slots=True)
class PluginMetrics:
    """Accumulated timing / resource counters for a plugin generation."""

    stage_seconds: dict[str, float] = field(default_factory=dict)
    cpu_seconds: float = 0.0
    hook_invocations: int = 0
    errors: int = 0
    last_error: str = ""
    installed_at: float = field(default_factory=time.time)
    started_at: float | None = None
    stopped_at: float | None = None
    memory_baseline: int | None = None

    def add_stage(self, label: str, seconds: float, cpu_seconds: float) -> None:
        self.stage_seconds[label] = self.stage_seconds.get(label, 0.0) + seconds
        self.cpu_seconds += cpu_seconds

    def record_error(self, error: BaseException | str) -> None:
        self.errors += 1
        self.last_error = repr(error) if isinstance(error, BaseException) else str(error)

    def hook(self, seconds: float, cpu_seconds: float) -> None:
        self.hook_invocations += 1
        self.cpu_seconds += cpu_seconds
        self.stage_seconds["hooks"] = self.stage_seconds.get("hooks", 0.0) + seconds

    def uptime(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.stopped_at or time.time()
        return max(0.0, end - self.started_at)


@dataclass(frozen=True, slots=True)
class PluginStatus:
    """An immutable, JSON-friendly view of one plugin."""

    name: str
    version: str
    state: PluginState
    description: str
    source: str
    requires: tuple[str, ...]
    provides: tuple[str, ...]
    capabilities: tuple[str, ...]
    hooks: int
    metrics: PluginMetrics
    memory_bytes: int | None
    sandbox: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.state in _ACTIVE_STATES

    @property
    def healthy(self) -> bool:
        return self.state != PluginState.FAILED and self.metrics.errors == 0

    @property
    def running(self) -> bool:
        return self.state == PluginState.RUNNING

    @property
    def uptime_seconds(self) -> float:
        return self.metrics.uptime()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "state": self.state.value,
            "enabled": self.enabled,
            "healthy": self.healthy,
            "running": self.running,
            "description": self.description,
            "source": self.source,
            "requires": list(self.requires),
            "provides": list(self.provides),
            "capabilities": list(self.capabilities),
            "hooks": self.hooks,
            "uptime_seconds": self.uptime_seconds,
            "sandbox": dict(self.sandbox),
            "metrics": {
                "stage_seconds": dict(self.metrics.stage_seconds),
                "cpu_seconds": self.metrics.cpu_seconds,
                "hook_invocations": self.metrics.hook_invocations,
                "errors": self.metrics.errors,
                "last_error": self.metrics.last_error,
                "installed_at": self.metrics.installed_at,
                "started_at": self.metrics.started_at,
                "stopped_at": self.metrics.stopped_at,
                "memory_bytes": self.memory_bytes,
            },
        }
