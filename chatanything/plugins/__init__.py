"""Plugin system: contracts, registry integration, loading and built-ins."""

from __future__ import annotations

from .base import PLUGIN_API_VERSION, Plugin, PluginContext, PluginMeta
from .hotplug import (
    ArchiveSource,
    DirectorySource,
    PluginManifest,
    PluginMetrics,
    PluginSandbox,
    PluginSource,
    PluginState,
    PluginStatus,
    RemoteSource,
    Requirement,
    SandboxPolicy,
    StagedPackage,
    Version,
)
from .loader import discover_entry_point_plugins, load_plugin
from .manager import InstalledPlugin, PluginManager

__all__ = [
    "Plugin",
    "PluginContext",
    "PluginMeta",
    "PluginManager",
    "InstalledPlugin",
    "PLUGIN_API_VERSION",
    "PluginManifest",
    "PluginMetrics",
    "PluginState",
    "PluginStatus",
    "PluginSandbox",
    "SandboxPolicy",
    "PluginSource",
    "DirectorySource",
    "ArchiveSource",
    "RemoteSource",
    "StagedPackage",
    "Requirement",
    "Version",
    "load_plugin",
    "discover_entry_point_plugins",
]
