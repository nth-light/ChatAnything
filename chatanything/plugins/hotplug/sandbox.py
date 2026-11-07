"""In-process plugin sandbox.

Isolation is provided at four levels:

* every package load gets a unique, private module namespace;
* a custom import hook blocks modules forbidden by the sandbox policy;
* plugins receive a scoped registry facade that can only mutate components
  owned by the plugin itself (and becomes inert once retired);
* lifecycle stages and hooks are time-limited and their errors are contained
  and attributed to the plugin.

This is a *cooperative* sandbox: it isolates well-behaved plugins and prevents
accidental collisions, but it is not a security boundary against deliberately
malicious code.  Run untrusted plugins in an external process/container.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence

from ...errors import (
    PluginLoadError,
    PluginSandboxError,
    PluginValidationError,
)
from ...lifecycle import Hook, LifecycleEngine, LifecycleEvent, LifecyclePoint
from ...registry import Capability, ComponentEntry, ComponentRegistry
from ..base import Plugin

#: Modules that are never importable by sandboxed plugins.
_PROCESS_MODULES = frozenset(
    {
        "subprocess",
        "multiprocessing",
        "ctypes",
        "_posixsubprocess",
        "pty",
        "resource",
        "cffi",
    }
)

_NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "urllib.request",
        "urllib3",
        "requests",
        "aiohttp",
        "httpx",
        "websockets",
    }
)


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Controls what a loaded plugin may import and how long hooks may run."""

    allow_network: bool = True
    allow_subprocess: bool = False
    deny_imports: frozenset[str] = frozenset()
    max_hook_seconds: float = 30.0

    def denied_modules(self) -> frozenset[str]:
        denied = set(self.deny_imports)
        if not self.allow_subprocess:
            denied |= _PROCESS_MODULES
        if not self.allow_network:
            denied |= _NETWORK_MODULES
        return frozenset(denied)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allow_network": self.allow_network,
            "allow_subprocess": self.allow_subprocess,
            "deny_imports": sorted(self.deny_imports),
            "max_hook_seconds": self.max_hook_seconds,
            "effective_denials": sorted(self.denied_modules()),
        }


def sandbox_policy_from_manifest(
    settings: Mapping[str, Any],
    *,
    base: SandboxPolicy | None = None,
) -> SandboxPolicy:
    """Merge manifest ``sandbox`` settings into a base policy."""

    policy = base or SandboxPolicy()
    updates: dict[str, Any] = {}
    if "allow_network" in settings:
        updates["allow_network"] = bool(settings["allow_network"])
    if "allow_subprocess" in settings:
        updates["allow_subprocess"] = bool(settings["allow_subprocess"])
    if "deny_imports" in settings:
        extra = settings["deny_imports"]
        if isinstance(extra, str):
            extra = (extra,)
        updates["deny_imports"] = policy.deny_imports | frozenset(str(item) for item in extra)
    if "max_hook_seconds" in settings:
        updates["max_hook_seconds"] = float(settings["max_hook_seconds"])
    return replace(policy, **updates)


class ImportGuard:
    """Blocks a configured set of modules for one plugin namespace."""

    def __init__(self, denied: Sequence[str]) -> None:
        self.denied = frozenset(denied)

    def check(self, module_name: str) -> None:
        top_level = module_name.split(".", 1)[0]
        for denied in self.denied:
            if module_name == denied or top_level == denied or module_name.startswith(denied + "."):
                raise ImportError(
                    f"plugin sandbox blocked import of {module_name!r} "
                    f"(policy denies {denied!r})"
                )

    def build_builtins(self) -> dict[str, Any]:
        original_import = builtins.__import__

        def guarded_import(
            name: str,
            globals: Mapping[str, Any] | None = None,
            locals: Mapping[str, Any] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> Any:
            if level == 0:
                self.check(name)
            return original_import(name, globals, locals, fromlist, level)

        namespace = dict(vars(builtins))
        namespace["__import__"] = guarded_import
        return namespace


class _SandboxedLoader(importlib.machinery.SourceFileLoader):
    """A source loader that injects guarded builtins before executing code."""

    def __init__(self, fullname: str, path: str, guarded_builtins: dict[str, Any]) -> None:
        super().__init__(fullname, path)
        self._guarded_builtins = guarded_builtins

    def exec_module(self, module: ModuleType) -> None:
        # Compile from source on every load instead of using cached ``.pyc``
        # files: hot updates frequently rewrite files in place with identical
        # size and a timestamp within the same second, which would silently
        # reuse stale bytecode.
        module.__dict__["__builtins__"] = self._guarded_builtins
        source = self.get_data(self.path)
        code = compile(source, self.path, "exec", dont_inherit=True)
        exec(code, module.__dict__)


class _PluginFinder(importlib.abc.MetaPathFinder):
    """Resolves submodules of one plugin namespace inside its package root."""

    def __init__(self, prefix: str, root: Path, guarded_builtins: dict[str, Any]) -> None:
        self.prefix = prefix
        self.root = root
        self.guarded_builtins = guarded_builtins

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith(self.prefix + "."):
            return None
        relative = fullname[len(self.prefix) + 1 :]
        parts = [part for part in relative.split(".") if part]
        if not parts:
            return None
        base = self.root.joinpath(*parts)
        module_file = base.with_suffix(".py")
        if module_file.is_file():
            loader = _SandboxedLoader(fullname, str(module_file), self.guarded_builtins)
            return importlib.util.spec_from_file_location(fullname, module_file, loader=loader)
        if base.is_dir():
            init_file = base / "__init__.py"
            if init_file.is_file():
                loader = _SandboxedLoader(fullname, str(init_file), self.guarded_builtins)
                return importlib.util.spec_from_file_location(
                    fullname,
                    init_file,
                    loader=loader,
                    submodule_search_locations=[str(base)],
                )
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(base)]
            return spec
        return None


class PluginSandbox:
    """An isolated module namespace plus policy enforcement for one plugin."""

    def __init__(
        self,
        name: str,
        root: str | Path,
        policy: SandboxPolicy,
    ) -> None:
        safe_name = "".join(ch if ch.isalnum() else "_" for ch in name)
        self.name = name
        self.root = Path(root)
        self.policy = policy
        self.prefix = f"_ca_plugin_{safe_name}_{uuid.uuid4().hex[:8]}"
        self.guard = ImportGuard(policy.denied_modules())
        self._finder = _PluginFinder(self.prefix, self.root, self.guard.build_builtins())
        self._root_module = self._install_namespace()
        self._disposed = False

    # ------------------------------------------------------------------
    def _install_namespace(self) -> ModuleType:
        spec = importlib.machinery.ModuleSpec(self.prefix, loader=None, is_package=True)
        spec.submodule_search_locations = [str(self.root)]
        module = ModuleType(self.prefix, doc="ChatAnything hot-plug namespace")
        module.__spec__ = spec
        module.__path__ = [str(self.root)]
        module.__package__ = self.prefix
        module.__file__ = str(self.root)
        module.__builtins__ = self.guard.build_builtins()
        sys.modules[self.prefix] = module
        sys.meta_path.insert(0, self._finder)
        return module

    # ------------------------------------------------------------------
    def load_plugin(self, entry: str) -> Plugin:
        """Import the declared entry module and instantiate the plugin."""

        module_part, _, attr = entry.partition(":")
        module_part = module_part.strip().replace("\\", "/").strip("/")
        if module_part.endswith(".py"):
            module_part = module_part[:-3]
        module_part = module_part.replace("/", ".")
        if not module_part:
            raise PluginValidationError(f"plugin {self.name!r} declares an empty entry module")
        fullname = f"{self.prefix}.{module_part}"
        try:
            module = importlib.import_module(fullname)
        except Exception as exc:  # noqa: BLE001 - plugin code is untrusted
            raise PluginLoadError(
                f"cannot import plugin entry {entry!r} for {self.name!r}: {exc}"
            ) from exc
        if attr:
            try:
                target = getattr(module, attr)
            except AttributeError as exc:
                raise PluginLoadError(
                    f"plugin entry {entry!r} has no attribute {attr!r}"
                ) from exc
        else:
            target = getattr(module, "PLUGIN", None) or getattr(module, "plugin", None)
            if target is None:
                raise PluginLoadError(
                    f"plugin entry {entry!r} exposes neither PLUGIN nor plugin"
                )
        return _instantiate(target, entry)

    # ------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "namespace": self.prefix,
            "root": str(self.root),
            "policy": self.policy.as_dict(),
        }

    def dispose(self) -> None:
        """Remove the namespace, finder and all imported plugin modules."""

        if self._disposed:
            return
        self._disposed = True
        try:
            sys.meta_path.remove(self._finder)
        except ValueError:
            pass
        for key in [
            name
            for name in sys.modules
            if name == self.prefix or name.startswith(self.prefix + ".")
        ]:
            sys.modules.pop(key, None)
        importlib.invalidate_caches()

    def __repr__(self) -> str:
        return f"<PluginSandbox {self.name} namespace={self.prefix}>"


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


class SandboxedRegistry:
    """A registry facade that can only mutate the owning plugin's components."""

    def __init__(self, registry: ComponentRegistry, plugin_name: str) -> None:
        self._registry = registry
        self._plugin = plugin_name
        self.active = True

    # ------------------------------------------------------------------
    def retire(self) -> None:
        self.active = False

    def reactivate(self) -> None:
        self.active = True

    def _owns(self, slot: Capability, name: str) -> bool:
        try:
            return self._registry.resolve_entry(slot, name).plugin == self._plugin
        except Exception:  # noqa: BLE001 - missing component means "not owned"
            return False

    def _forbid(self, action: str, target: str) -> None:
        raise PluginSandboxError(
            f"plugin {self._plugin!r} cannot {action} {target!r}: owned by another plugin"
        )

    # ------------------------------------------------------------------
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
    ) -> ComponentEntry[Any] | None:
        if not self.active:
            return None
        if plugin and plugin != self._plugin:
            self._forbid("register a component for", plugin)
        return self._registry.register(
            slot,
            name,
            component,
            plugin=self._plugin,
            priority=priority,
            default=default,
            replace=replace,
            metadata=metadata,
        )

    def set_default(self, slot: Capability, name: str) -> None:
        if not self.active:
            return
        if not self._owns(slot, name):
            self._forbid("set the default component", f"{slot.value}:{name}")
        self._registry.set_default(slot, name)

    def remove(self, slot: Capability, name: str) -> None:
        if not self.active:
            return
        if not self._owns(slot, name):
            self._forbid("remove", f"{slot.value}:{name}")
        self._registry.remove(slot, name)

    def unregister_plugin(self, plugin: str | None = None) -> int:
        if not self.active:
            return 0
        target = plugin or self._plugin
        if target != self._plugin:
            self._forbid("unregister", target)
        return self._registry.unregister_plugin(target)

    # Read-only operations -------------------------------------------------
    def resolve(self, slot: Capability, name: str | None = None) -> Any:
        return self._registry.resolve(slot, name)

    def resolve_entry(self, slot: Capability, name: str | None = None) -> ComponentEntry[Any]:
        return self._registry.resolve_entry(slot, name)

    def resolve_all(self, slot: Capability) -> tuple[Any, ...]:
        return self._registry.resolve_all(slot)

    def entries(self, slot: Capability) -> tuple[ComponentEntry[Any], ...]:
        return self._registry.entries(slot)

    def names(self, slot: Capability) -> tuple[str, ...]:
        return self._registry.names(slot)

    def contains(self, slot: Capability, name: str) -> bool:
        return self._registry.contains(slot, name)

    def default_name(self, slot: Capability) -> str | None:
        return self._registry.default_name(slot)

    def snapshot(self) -> dict[str, tuple[str, ...]]:
        return self._registry.snapshot()

    def typed(self, slot: Capability, expected: type[Any], name: str | None = None) -> Any:
        return self._registry.typed(slot, expected, name)


class SandboxedLifecycle:
    """A lifecycle facade that scopes subscriptions to the owning plugin."""

    def __init__(self, engine: LifecycleEngine, plugin_name: str) -> None:
        self._engine = engine
        self._owner = f"plugin:{plugin_name}"

    def subscribe(
        self,
        point: LifecyclePoint,
        handler: Hook,
        *,
        owner: str | None = None,
        priority: int = 100,
        once: bool = False,
    ) -> None:
        self._engine.subscribe(
            point,
            handler,
            owner=self._owner,
            priority=priority,
            once=once,
        )

    def unsubscribe(
        self,
        *,
        owner: str | None = None,
        point: LifecyclePoint | None = None,
        handler: Callable[[LifecycleEvent], Any] | None = None,
    ) -> int:
        return self._engine.unsubscribe(owner=self._owner, point=point, handler=handler)

    def subscribers(self, point: LifecyclePoint) -> tuple[Hook, ...]:
        return self._engine.subscribers(point)

    async def emit(self, point: LifecyclePoint, **payload: Any) -> LifecycleEvent:
        return await self._engine.emit(point, **payload)

    def last_event(self, point: LifecyclePoint | None = None) -> LifecycleEvent | None:
        return self._engine.last_event(point)

    @property
    def history(self) -> tuple[LifecycleEvent, ...]:
        return self._engine.history

    # Read-only / observation surface --------------------------------------
    @property
    def state(self) -> Any:
        return self._engine.state

    @property
    def hook_errors(self) -> list[tuple[LifecyclePoint, str]]:
        return self._engine.hook_errors

    @property
    def logger(self) -> Any:
        return self._engine.logger

    @property
    def raise_on_hook_error(self) -> bool:
        return self._engine.raise_on_hook_error

    def can_transition(self, state: Any) -> bool:
        return self._engine.can_transition(state)
