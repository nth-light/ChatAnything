"""Hot-plug building blocks: manifests, sources, sandbox, dependencies, states."""

from __future__ import annotations

from .dependencies import Node, order_nodes, validate_requirements
from .manifest import (
    MANIFEST_FILES,
    HOST_API_VERSION,
    PluginManifest,
    find_manifest,
    load_manifest,
)
from .resources import ResourceMeter
from .sandbox import (
    PluginSandbox,
    SandboxPolicy,
    SandboxedLifecycle,
    SandboxedRegistry,
    sandbox_policy_from_manifest,
)
from .sources import (
    ArchiveSource,
    DirectorySource,
    PluginSource,
    RemoteSource,
    StagedPackage,
    is_archive,
    is_url,
    source_from,
)
from .state import PluginMetrics, PluginState, PluginStatus
from .versioning import (
    Requirement,
    Specifier,
    Version,
    parse_requirement,
    parse_requirements,
    parse_specifiers,
    parse_version,
)

__all__ = [
    "Node",
    "order_nodes",
    "validate_requirements",
    "MANIFEST_FILES",
    "HOST_API_VERSION",
    "PluginManifest",
    "find_manifest",
    "load_manifest",
    "ResourceMeter",
    "PluginSandbox",
    "SandboxPolicy",
    "SandboxedLifecycle",
    "SandboxedRegistry",
    "sandbox_policy_from_manifest",
    "ArchiveSource",
    "DirectorySource",
    "PluginSource",
    "RemoteSource",
    "StagedPackage",
    "is_archive",
    "is_url",
    "source_from",
    "PluginMetrics",
    "PluginState",
    "PluginStatus",
    "Requirement",
    "Specifier",
    "Version",
    "parse_requirement",
    "parse_requirements",
    "parse_specifiers",
    "parse_version",
]
