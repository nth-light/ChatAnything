"""Plugin discovery and import.

Supported reference formats::

    "my_package.my_module:MyPlugin"      # explicit class
    "my_package.my_module:plugin"        # module-level plugin instance
    "my_package.my_module"               # looks for PLUGIN / plugin

Third-party packages can also advertise plugins through the
``chatanything.plugins`` entry-point group.
"""

from __future__ import annotations

import importlib
import inspect
from importlib.metadata import entry_points
from typing import Iterable

from ..errors import PluginLoadError
from .base import Plugin


def _instantiate(target: object, reference: str) -> Plugin:
    if isinstance(target, Plugin):
        return target
    if inspect.isclass(target) and issubclass(target, Plugin):
        return target()
    if callable(target):
        candidate = target()
        if isinstance(candidate, Plugin):
            return candidate
    raise PluginLoadError(f"{reference!r} does not resolve to a Plugin")


def load_plugin(reference: str) -> Plugin:
    """Import and instantiate a plugin from a ``module:attr`` reference."""

    module_name, _, attr_name = reference.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginLoadError(f"cannot import plugin module {module_name!r}: {exc}") from exc

    if attr_name:
        try:
            target = getattr(module, attr_name)
        except AttributeError as exc:
            raise PluginLoadError(
                f"module {module_name!r} has no attribute {attr_name!r}"
            ) from exc
    else:
        target = getattr(module, "PLUGIN", None) or getattr(module, "plugin", None)
        if target is None:
            raise PluginLoadError(
                f"module {module_name!r} exposes neither PLUGIN nor plugin"
            )
    return _instantiate(target, reference)


def discover_entry_point_plugins(
    group: str = "chatanything.plugins",
) -> Iterable[Plugin]:
    """Yield plugins advertised through package entry points."""

    try:
        discovered = entry_points(group=group)
    except TypeError:  # Python < 3.10 compatibility path
        discovered = entry_points().get(group, [])  # type: ignore[union-attr]
    for entry_point in discovered:
        try:
            target = entry_point.load()
            yield _instantiate(target, f"{entry_point.module}:{entry_point.name}")
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            raise PluginLoadError(f"failed to load entry point {entry_point.name!r}: {exc}") from exc
