"""Standard plugin contract.

A plugin is the *only* extension unit in ChatAnything.  It can:

* contribute one or more capabilities to the registry;
* observe lifecycle points through hooks;
* keep its own configuration;
* clean up deterministically on uninstall.

The framework core never imports a concrete plugin implementation.
"""

from __future__ import annotations

import logging
from abc import ABC
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from ..lifecycle import Hook, LifecycleEngine, LifecyclePoint
from ..registry import ComponentRegistry

if TYPE_CHECKING:
    from .hotplug.manifest import PluginManifest

#: Host plugin ABI version.  Manifests declare which ABI they require.
PLUGIN_API_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class PluginMeta:
    """Immutable plugin identity and dependency information.

    ``requires`` may contain plain names (``"storage.sqlite"``) or version
    constraints (``"storage.sqlite>=1.2,<2.0"``).
    """

    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    priority: int = 10
    homepage: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    api_version: str = ">=1.0,<2.0"
    python_requires: str = ">=3.10"


@dataclass(slots=True)
class PluginContext:
    """Everything a plugin is allowed to touch at install time."""

    name: str
    config: Mapping[str, Any]
    container: ComponentRegistry
    lifecycle: LifecycleEngine
    logger: logging.Logger
    manifest: "PluginManifest | None" = None
    sandbox: Mapping[str, Any] | None = None

    def setting(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)


class Plugin(ABC):
    """Base class for all framework and third-party plugins.

    Lifecycle stages (all optional, overridden by concrete plugins):

    1. :meth:`on_initialize` -- allocate resources and prepare state;
    2. :meth:`on_start` -- activate the plugin (called after registration);
    3. :meth:`on_stop` -- quiesce work without releasing resources;
    4. :meth:`on_destroy` -- release everything acquired during the lifecycle.

    ``install``/``uninstall`` remain the declaration points where capabilities
    are registered and external resources released.  They run inside the
    initialize and destroy stages respectively.

    ``on_*`` names are used instead of bare ``start``/``stop`` so plugins that
    also implement :class:`~chatanything.plugins.interfaces.EventSource`
    (which owns ``start(emit)`` / ``stop()``) do not collide.
    """

    meta: PluginMeta

    async def install(self, context: PluginContext) -> None:
        """Register capabilities and (optionally) lifecycle hooks."""

    async def uninstall(self, context: PluginContext) -> None:
        """Release external resources. Registry cleanup is automatic."""

    async def on_initialize(self, context: PluginContext) -> None:
        """Initialization hook: prepare state before the plugin is started."""

    async def on_start(self, context: PluginContext) -> None:
        """Start hook: activate background work or warm caches."""

    async def on_stop(self, context: PluginContext) -> None:
        """Stop hook: quiesce work while keeping allocated resources."""

    async def on_destroy(self, context: PluginContext) -> None:
        """Destroy hook: release resources not covered by ``uninstall``."""

    def hooks(self) -> Mapping[LifecyclePoint, Hook] | None:
        """Return lifecycle hooks to subscribe automatically.

        Hooks declared here are owned by the plugin and removed on uninstall.
        Plugins may also call ``context.lifecycle.subscribe`` directly.
        """

        return None

    def __repr__(self) -> str:
        return f"<Plugin {self.meta.name}@{self.meta.version}>"
