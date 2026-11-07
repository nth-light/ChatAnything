"""Harness configuration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class HarnessConfig:
    """Runtime knobs shared by the core and built-in plugins.

    All values have safe defaults so the harness can run without an external
    LLM.  Production deployments will typically replace ``defaults`` and
    ``plugin_config`` instead of editing core code.
    """

    agent_name: str = "ChatAnything"

    # Plugin loading
    install_builtin_plugins: bool = True
    plugin_modules: list[str] = field(default_factory=list)
    plugin_config: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Directories scanned for hot-pluggable plugin packages (plugin.json/zip).
    plugin_directories: list[str] = field(default_factory=list)
    discover_entry_points: bool = False
    entry_point_group: str = "chatanything.plugins"
    # Enables per-plugin tracemalloc-based memory reporting.
    plugin_resource_monitoring: bool = False

    # Explicit default components per capability, e.g. {"llm": "openai"}
    defaults: dict[str, str] = field(default_factory=dict)
    default_channel: str = "console"

    # Router batching
    batch_window_seconds: float = 0.05
    batch_max_triggers: int = 16

    # Worker execution
    max_workers: int = 4
    worker_idle_timeout: float = 45.0
    max_steps: int = 6
    tool_timeout_seconds: float = 30.0

    # Context management
    context_token_budget: int = 6000
    inline_token_limit: int = 800

    # External control loop
    watchdog_interval: float = 15.0
    watchdog_stall_seconds: float = 120.0
    watchdog_enabled: bool = True

    # Lifecycle / infrastructure
    auto_start_sources: bool = True
    speculation_enabled: bool = False
    raise_on_hook_error: bool = False
    log_level: int = logging.INFO
    log_format: str = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

    def plugin_settings(self, name: str) -> dict[str, Any]:
        """Return the configuration block for a named plugin."""

        return dict(self.plugin_config.get(name, {}))
