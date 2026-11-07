"""Plugin dependency resolution, version conflict detection and cycle checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ...errors import (
    PluginDependencyCycleError,
    PluginDependencyError,
    PluginVersionConflictError,
)
from .versioning import Requirement, Version, parse_version


@dataclass(slots=True)
class Node:
    """One plugin participating in a dependency resolution batch."""

    name: str
    version: str
    requirements: tuple[Requirement, ...] = ()
    priority: int = 10
    index: int = 0


def order_nodes(
    nodes: Mapping[str, Node],
    installed_versions: Mapping[str, str],
) -> list[str]:
    """Validate a plugin batch and return a dependency-first install order.

    Raises:
        PluginDependencyError: a dependency is neither in the batch nor installed.
        PluginVersionConflictError: constraints cannot be satisfied by the
            available candidate or installed version.
        PluginDependencyCycleError: the batch contains a dependency cycle.
    """

    for name, node in nodes.items():
        if name != node.name:
            raise PluginDependencyError(f"dependency node key mismatch: {name!r}")
        parse_version(node.version)

    constraints: dict[str, list[tuple[str, Requirement]]] = {}
    for node in nodes.values():
        for requirement in node.requirements:
            if requirement.name == node.name:
                raise PluginDependencyCycleError(
                    f"plugin {node.name!r} cannot depend on itself"
                )
            if requirement.name not in nodes and requirement.name not in installed_versions:
                raise PluginDependencyError(
                    f"plugin {node.name!r} requires missing plugin {requirement.name!r}"
                )
            constraints.setdefault(requirement.name, []).append((node.name, requirement))

    # Version conflict detection: every candidate/installed version must satisfy
    # all constraints declared by the whole batch.
    for dependency, entries in constraints.items():
        if dependency in nodes:
            candidate = Version(nodes[dependency].version)
        else:
            candidate = Version(installed_versions[dependency])
        for requester, requirement in entries:
            if not requirement.matches(candidate):
                raise PluginVersionConflictError(
                    f"plugin {requester!r} requires {requirement}, "
                    f"but {dependency!r} is available as {candidate}"
                )

    # Kahn topological sort over intra-batch edges.
    pending = set(nodes)
    resolved: list[str] = []
    while pending:
        ready = [
            name
            for name in pending
            if all(
                requirement.name not in pending
                for requirement in nodes[name].requirements
            )
        ]
        if not ready:
            names = ", ".join(sorted(pending))
            raise PluginDependencyCycleError(
                f"circular plugin dependencies detected among: {names}"
            )
        ready.sort(key=lambda name: (nodes[name].priority, nodes[name].index, name))
        for name in ready:
            pending.remove(name)
            resolved.append(name)
    return resolved


def validate_requirements(
    requirements: tuple[Requirement, ...],
    *,
    plugin_name: str,
    installed_versions: Mapping[str, str],
) -> None:
    """Validate one plugin's requirements against already-installed plugins."""

    for requirement in requirements:
        version = installed_versions.get(requirement.name)
        if version is None:
            raise PluginDependencyError(
                f"plugin {plugin_name!r} requires missing plugin {requirement.name!r}"
            )
        if not requirement.matches(version):
            raise PluginVersionConflictError(
                f"plugin {plugin_name!r} requires {requirement}, "
                f"but {requirement.name!r} is installed as {version}"
            )
