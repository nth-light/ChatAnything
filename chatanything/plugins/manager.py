"""Hot-pluggable plugin manager.

Responsibilities:

* install plugins loaded from a reference, a local package or a remote archive;
* run the standard lifecycle (``install`` -> ``on_initialize`` -> ``on_start``
  -> ``on_stop`` -> ``on_destroy`` -> ``uninstall``);
* resolve dependencies by version, detect conflicts and cycles;
* isolate plugins inside a sandbox and keep per-plugin metrics;
* perform enable/disable/reload/update as atomic transactions that roll back
  registry and lifecycle-hook state on failure;
* expose a live status snapshot for monitoring endpoints.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..errors import (
    PluginError,
    PluginLoadError,
    PluginSandboxError,
    PluginSourceError,
    PluginStateError,
    PluginValidationError,
)
from ..lifecycle import Hook, LifecycleEngine, LifecycleEvent, LifecyclePoint
from ..registry import Capability, ComponentEntry, ComponentRegistry
from .base import Plugin, PluginContext, PluginMeta
from .hotplug.dependencies import Node, order_nodes, validate_requirements
from .hotplug.manifest import (
    PluginManifest,
    find_manifest,
    load_manifest,
)
from .hotplug.resources import ResourceMeter
from .hotplug.sandbox import (
    PluginSandbox,
    SandboxPolicy,
    SandboxedLifecycle,
    SandboxedRegistry,
    sandbox_policy_from_manifest,
)
from .hotplug.sources import (
    PluginSource,
    StagedPackage,
    is_archive,
    source_from,
)
from .hotplug.state import PluginMetrics, PluginState, PluginStatus
from .loader import discover_entry_point_plugins, load_plugin


@dataclass(slots=True)
class InstalledPlugin:
    """Bookkeeping for one successfully installed plugin generation."""

    plugin: Plugin
    context: PluginContext
    hooks: int = 0
    order: int = 0
    state: PluginState = PluginState.REGISTERED
    manifest: PluginManifest | None = None
    staged: StagedPackage | None = None
    sandbox: PluginSandbox | None = None
    reference: str | None = None
    metrics: PluginMetrics = field(default_factory=PluginMetrics)
    config: dict[str, Any] = field(default_factory=dict)
    detached_entries: tuple[ComponentEntry[Any], ...] = ()
    detached_defaults: tuple[tuple[Capability, str], ...] = ()
    policy: SandboxPolicy | None = None
    facade: SandboxedRegistry | None = None
    generation: int = 1

    @property
    def meta(self) -> PluginMeta:
        return self.plugin.meta

    @property
    def enabled(self) -> bool:
        return self.state in (
            PluginState.INITIALIZED,
            PluginState.STARTING,
            PluginState.RUNNING,
            PluginState.STOPPING,
        )


@dataclass(slots=True)
class _Candidate:
    """A validated plugin instance waiting to be activated."""

    plugin: Plugin
    manifest: PluginManifest
    config: dict[str, Any] = field(default_factory=dict)
    staged: StagedPackage | None = None
    sandbox: PluginSandbox | None = None
    reference: str | None = None
    policy: SandboxPolicy | None = None
    consumed: bool = False

    def dispose(self) -> None:
        if self.sandbox is not None:
            self.sandbox.dispose()
            self.sandbox = None
        if self.staged is not None:
            self.staged.cleanup()
            self.staged = None


class PluginManager:
    """Install, load, enable, disable, reload and monitor plugins at runtime."""

    def __init__(
        self,
        container: ComponentRegistry,
        lifecycle: LifecycleEngine,
        *,
        logger: logging.Logger | None = None,
        plugin_config: Mapping[str, Mapping[str, Any]] | None = None,
        sandbox_policy: SandboxPolicy | None = None,
        resource_monitoring: bool = False,
    ) -> None:
        self.container = container
        self.lifecycle = lifecycle
        self.logger = logger or logging.getLogger("chatanything.plugins")
        self.plugin_config = plugin_config or {}
        self.sandbox_policy = sandbox_policy or SandboxPolicy()
        self.meter = ResourceMeter(track_memory=resource_monitoring)
        self._installed: dict[str, InstalledPlugin] = {}
        self._order = 0
        self._generation = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------
    async def install(
        self,
        plugin: Plugin,
        *,
        config: Mapping[str, Any] | None = None,
        autostart: bool = True,
    ) -> InstalledPlugin:
        """Install an already-instantiated plugin (no module isolation)."""

        candidate = _Candidate(
            plugin=plugin,
            manifest=PluginManifest.from_meta(plugin.meta, origin="in-memory"),
            config=dict(config or {}),
        )
        async with self._lock:
            self._ensure_absent(plugin.meta.name)
            return await self._activate_candidate(candidate, autostart=autostart)

    async def install_all(
        self,
        plugins: Iterable[Plugin],
        *,
        configs: Mapping[str, Mapping[str, Any]] | None = None,
        autostart: bool = True,
    ) -> list[InstalledPlugin]:
        """Install a batch atomically, honoring dependencies and priority."""

        settings = configs or {}
        candidates = [
            _Candidate(
                plugin=plugin,
                manifest=PluginManifest.from_meta(plugin.meta, origin="in-memory"),
                config=dict(settings.get(plugin.meta.name, {})),
            )
            for plugin in plugins
        ]
        async with self._lock:
            return await self._activate_batch(candidates, autostart=autostart)

    async def load_references(
        self,
        references: Iterable[str],
        *,
        configs: Mapping[str, Mapping[str, Any]] | None = None,
        autostart: bool = True,
    ) -> list[InstalledPlugin]:
        """Load plugins by ``module:Class`` references (host imports them)."""

        settings = configs or {}
        candidates = []
        for reference in references:
            plugin = await asyncio.to_thread(load_plugin, reference)
            candidates.append(
                _Candidate(
                    plugin=plugin,
                    manifest=PluginManifest.from_meta(
                        plugin.meta, origin=f"reference:{reference}"
                    ),
                    config=dict(settings.get(plugin.meta.name, {})),
                    reference=reference,
                )
            )
        async with self._lock:
            return await self._activate_batch(candidates, autostart=autostart)

    async def discover(
        self,
        group: str = "chatanything.plugins",
        *,
        configs: Mapping[str, Mapping[str, Any]] | None = None,
        autostart: bool = True,
    ) -> list[InstalledPlugin]:
        """Install every plugin advertised through entry points."""

        try:
            plugins = await asyncio.to_thread(lambda: list(discover_entry_point_plugins(group)))
        except PluginLoadError:
            raise
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            raise PluginLoadError(f"plugin entry-point discovery failed: {exc}") from exc
        return await self.install_all(plugins, configs=configs, autostart=autostart)

    # ------------------------------------------------------------------
    # Hot loading from packages
    # ------------------------------------------------------------------
    async def load(
        self,
        source: str | Path | PluginSource,
        *,
        config: Mapping[str, Any] | None = None,
        autostart: bool = True,
    ) -> InstalledPlugin:
        """Load, validate, sandbox and install one plugin from any source."""

        candidate = await asyncio.to_thread(self._prepare_source, source, config)
        async with self._lock:
            try:
                self._ensure_absent(candidate.manifest.name)
                await self.lifecycle.emit(
                    LifecyclePoint.PLUGIN_VALIDATED,
                    plugin=candidate.manifest.name,
                    version=candidate.manifest.version,
                    source=candidate.staged.origin if candidate.staged else candidate.reference,
                )
                return await self._activate_candidate(candidate, autostart=autostart)
            except Exception:
                candidate.dispose()
                raise

    async def load_many(
        self,
        sources: Iterable[str | Path | PluginSource],
        *,
        configs: Mapping[str, Mapping[str, Any]] | None = None,
        autostart: bool = True,
    ) -> list[InstalledPlugin]:
        """Load and install several packages atomically in dependency order."""

        settings = configs or {}
        candidates: list[_Candidate] = []
        try:
            for source in sources:
                candidate = await asyncio.to_thread(self._prepare_source, source, None)
                candidate.config = dict(settings.get(candidate.manifest.name, {}))
                candidates.append(candidate)
        except Exception:
            for candidate in candidates:
                candidate.dispose()
            raise
        async with self._lock:
            return await self._activate_batch(candidates, autostart=autostart)

    async def load_directory(
        self,
        path: str | Path,
        *,
        configs: Mapping[str, Mapping[str, Any]] | None = None,
        autostart: bool = True,
    ) -> list[InstalledPlugin]:
        """Load one package directory or every package directly inside it."""

        root = Path(path)
        if not root.exists():
            raise PluginSourceError(f"plugin directory not found: {root}")
        if _has_manifest(root):
            return await self.load_many([root], configs=configs, autostart=autostart)
        children = [
            child
            for child in sorted(root.iterdir(), key=lambda item: item.name)
            if (child.is_dir() and _has_manifest(child)) or (child.is_file() and is_archive(child))
        ]
        if not children:
            raise PluginSourceError(f"no plugin packages found in {root}")
        return await self.load_many(children, configs=configs, autostart=autostart)

    # ------------------------------------------------------------------
    # Removal / enable / disable
    # ------------------------------------------------------------------
    async def uninstall(self, name: str) -> None:
        """Uninstall a plugin if present (idempotent, used during shutdown)."""

        async with self._lock:
            if name in self._installed:
                await self._deactivate(name, emit=True, force=False)

    async def unload(self, name: str) -> None:
        """Unload a plugin; raises when it is not installed."""

        async with self._lock:
            if name not in self._installed:
                raise PluginError(f"plugin not installed: {name}")
            await self._deactivate(name, emit=True, force=False)

    async def disable(self, name: str) -> InstalledPlugin:
        """Stop a plugin and withdraw its capabilities without unloading it."""

        async with self._lock:
            record = self._require(name)
            if record.state == PluginState.DISABLED:
                return record

            was_running = record.state == PluginState.RUNNING
            registry_snapshot = self.container.snapshot_state()
            subscription_snapshot = self.lifecycle.snapshot_subscriptions()
            try:
                if was_running:
                    try:
                        await self._stop_record(record)
                    except Exception:
                        await self._try_restart(record)
                        raise
                self._withdraw_capabilities(record)
            except Exception:
                self.container.restore_state(registry_snapshot)
                self.lifecycle.restore_subscriptions(subscription_snapshot)
                if record.facade is not None:
                    record.facade.reactivate()
                record.state = PluginState.RUNNING if was_running else record.state
                raise
            record.state = PluginState.DISABLED
            record.metrics.stopped_at = time.time()
            await self.lifecycle.emit(
                LifecyclePoint.PLUGIN_DISABLED,
                plugin=name,
                version=record.meta.version,
            )
            self.logger.info("disabled plugin %s", name)
            return record

    async def enable(self, name: str) -> InstalledPlugin:
        """Re-activate a stopped or disabled plugin without reloading it."""

        async with self._lock:
            record = self._require(name)
            if record.state == PluginState.RUNNING:
                return record
            if record.state not in (
                PluginState.DISABLED,
                PluginState.INITIALIZED,
                PluginState.STOPPED,
            ):
                raise PluginStateError(
                    f"plugin {name!r} cannot be enabled from state {record.state.value}"
                )

            registry_snapshot = self.container.snapshot_state()
            subscription_snapshot = self.lifecycle.snapshot_subscriptions()
            previous_state = record.state
            try:
                if record.state == PluginState.DISABLED:
                    if record.facade is not None:
                        record.facade.reactivate()
                    self._restore_capabilities(record)
                    self._subscribe_hooks(record)
                    record.state = PluginState.INITIALIZED
                await self._start_record(record)
            except Exception:
                self.container.restore_state(registry_snapshot)
                self.lifecycle.restore_subscriptions(subscription_snapshot)
                if record.facade is not None:
                    record.facade.retire()
                record.state = previous_state
                raise
            await self.lifecycle.emit(
                LifecyclePoint.PLUGIN_ENABLED,
                plugin=name,
                version=record.meta.version,
            )
            self.logger.info("enabled plugin %s", name)
            return record

    async def reload(self, name: str) -> InstalledPlugin:
        """Atomically replace a package plugin with a fresh instance."""

        async with self._lock:
            record = self._require(name)
            candidate = await asyncio.to_thread(self._candidate_from_record, record)
            candidate.config = dict(record.config)
            if candidate.manifest.name != name:
                candidate.dispose()
                raise PluginValidationError(
                    f"reload source resolves to {candidate.manifest.name!r}, expected {name!r}"
                )
            return await self._swap(name, candidate)

    async def update(
        self,
        name: str,
        source: str | Path | PluginSource,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> InstalledPlugin:
        """Atomically replace an installed plugin with a new package/version."""

        candidate = await asyncio.to_thread(self._prepare_source, source, config)
        async with self._lock:
            record = self._installed.get(name)
            if record is None:
                candidate.dispose()
                raise PluginError(f"plugin not installed: {name}")
            if candidate.manifest.name != name:
                candidate.dispose()
                raise PluginValidationError(
                    f"update source declares {candidate.manifest.name!r}, expected {name!r}"
                )
            if config is None:
                candidate.config = dict(record.config)
            return await self._swap(name, candidate)

    async def shutdown(self) -> None:
        """Uninstall every plugin in reverse installation order."""

        async with self._lock:
            for name in reversed(list(self._installed.keys())):
                try:
                    await self._deactivate(name, emit=True, force=True)
                except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                    self.logger.exception("failed to uninstall plugin %s: %s", name, exc)

    # ------------------------------------------------------------------
    # Bulk lifecycle
    # ------------------------------------------------------------------
    async def start_all(self) -> None:
        """Start every initialized plugin; rolls back started ones on failure."""

        async with self._lock:
            started: list[str] = []
            for name in self._ordered_names():
                record = self._installed[name]
                if record.state != PluginState.INITIALIZED:
                    continue
                try:
                    await self._start_record(record)
                    started.append(name)
                except Exception:
                    for started_name in reversed(started):
                        try:
                            await self._stop_record(self._installed[started_name])
                        except Exception:  # noqa: BLE001
                            self.logger.exception("rollback stop failed for %s", started_name)
                    raise

    async def stop_all(self) -> None:
        """Stop every running plugin, continuing on individual failures."""

        async with self._lock:
            for name in reversed(self._ordered_names()):
                record = self._installed[name]
                if record.state != PluginState.RUNNING:
                    continue
                try:
                    await self._stop_record(record)
                except Exception:  # noqa: BLE001 - stop is best effort
                    self.logger.exception("failed to stop plugin %s", name)

    # ------------------------------------------------------------------
    # Introspection / monitoring
    # ------------------------------------------------------------------
    def get(self, name: str) -> InstalledPlugin | None:
        return self._installed.get(name)

    def installed_names(self) -> tuple[str, ...]:
        return tuple(self._installed.keys())

    def running_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, record in self._installed.items()
            if record.state == PluginState.RUNNING
        )

    def status(self) -> list[PluginStatus]:
        """A live snapshot of every loaded plugin including resource usage."""

        result: list[PluginStatus] = []
        for name in self._ordered_names():
            record = self._installed[name]
            capabilities = self._capability_labels(record)
            memory = self.meter.delta(record.metrics.memory_baseline)
            result.append(
                PluginStatus(
                    name=name,
                    version=record.meta.version,
                    state=record.state,
                    description=record.manifest.description if record.manifest else "",
                    source=_record_source(record),
                    requires=record.manifest.requires_names if record.manifest else (),
                    provides=record.meta.provides,
                    capabilities=capabilities,
                    hooks=record.hooks,
                    metrics=record.metrics,
                    memory_bytes=memory,
                    sandbox=record.sandbox.describe() if record.sandbox else {},
                )
            )
        return result

    def status_of(self, name: str) -> PluginStatus | None:
        for status in self.status():
            if status.name == name:
                return status
        return None

    def metrics(self, name: str) -> dict[str, Any] | None:
        status = self.status_of(name)
        return status.to_dict()["metrics"] if status else None

    def process_usage(self) -> dict[str, Any]:
        return self.meter.process_usage()

    def describe(self) -> list[dict[str, Any]]:
        """Backwards-compatible JSON-friendly description of all plugins."""

        return [status.to_dict() for status in self.status()]

    # ------------------------------------------------------------------
    # Transaction internals
    # ------------------------------------------------------------------
    async def _activate_batch(
        self,
        candidates: list[_Candidate],
        *,
        autostart: bool,
    ) -> list[InstalledPlugin]:
        if not candidates:
            return []

        mapping: dict[str, _Candidate] = {}
        for candidate in candidates:
            name = candidate.manifest.name
            if name in mapping or name in self._installed:
                for pending in candidates:
                    pending.dispose()
                raise PluginError(f"plugin already installed: {name}")
            mapping[name] = candidate

        nodes = {
            name: Node(
                name=name,
                version=candidate.manifest.version,
                requirements=candidate.manifest.requires,
                priority=candidate.manifest.priority,
                index=index,
            )
            for index, (name, candidate) in enumerate(mapping.items())
        }
        try:
            order = order_nodes(
                nodes,
                {name: record.meta.version for name, record in self._installed.items()},
            )
        except Exception:
            for pending in candidates:
                pending.dispose()
            raise

        registry_snapshot = self.container.snapshot_state()
        subscription_snapshot = self.lifecycle.snapshot_subscriptions()
        installed_now: list[str] = []
        try:
            for name in order:
                await self._activate_candidate(
                    mapping[name],
                    autostart=autostart,
                    use_snapshot=False,
                )
                installed_now.append(name)
        except Exception:
            for installed_name in reversed(installed_now):
                try:
                    await self._deactivate(installed_name, emit=False, force=True)
                except Exception:  # noqa: BLE001
                    self.logger.exception("batch rollback failed for %s", installed_name)
            for candidate in candidates:
                candidate.dispose()
            self.container.restore_state(registry_snapshot)
            self.lifecycle.restore_subscriptions(subscription_snapshot)
            raise
        return [self._installed[name] for name in order]

    async def _activate_candidate(
        self,
        candidate: _Candidate,
        *,
        autostart: bool,
        use_snapshot: bool = True,
    ) -> InstalledPlugin:
        manifest = candidate.manifest
        plugin = candidate.plugin
        name = manifest.name

        manifest.validate_host()
        if plugin.meta.name != name:
            raise PluginValidationError(
                f"plugin class declares name {plugin.meta.name!r}, manifest declares {name!r}"
            )
        if plugin.meta.version != manifest.version:
            raise PluginValidationError(
                f"plugin {name!r} version mismatch: manifest {manifest.version}, "
                f"class {plugin.meta.version}"
            )
        validate_requirements(
            manifest.requires,
            plugin_name=name,
            installed_versions={
                installed: record.meta.version
                for installed, record in self._installed.items()
                if installed != name
            },
        )

        registry_snapshot = self.container.snapshot_state() if use_snapshot else None
        subscription_snapshot = (
            self.lifecycle.snapshot_subscriptions() if use_snapshot else None
        )

        config = dict(candidate.config or self.plugin_config.get(name, {}))
        facade = SandboxedRegistry(self.container, name)
        scoped = SandboxedLifecycle(self.lifecycle, name)
        context = PluginContext(
            name=name,
            config=MappingProxyType(config),
            container=facade,  # type: ignore[arg-type]
            lifecycle=scoped,  # type: ignore[arg-type]
            logger=logging.getLogger(f"chatanything.plugin.{name}"),
            manifest=manifest,
            sandbox=candidate.sandbox.describe() if candidate.sandbox else None,
        )
        self._generation += 1
        self._order += 1
        record = InstalledPlugin(
            plugin=plugin,
            context=context,
            order=self._order,
            state=PluginState.REGISTERED,
            manifest=manifest,
            staged=candidate.staged,
            sandbox=candidate.sandbox,
            reference=candidate.reference,
            config=config,
            policy=candidate.policy,
            facade=facade,
            generation=self._generation,
        )
        record.metrics.memory_baseline = self.meter.baseline()

        try:
            await self._invoke(record, "install", plugin.install, context)
            record.state = PluginState.INITIALIZING
            self._subscribe_hooks(record)
            await self._invoke(record, "initialize", plugin.on_initialize, context)
            record.state = PluginState.INITIALIZED
            await self.lifecycle.emit(
                LifecyclePoint.PLUGIN_INITIALIZED,
                plugin=name,
                version=manifest.version,
            )
            if autostart:
                await self._start_record(record)
        except Exception as exc:
            await self._cleanup_failed(record, candidate)
            if registry_snapshot is not None:
                self.container.restore_state(registry_snapshot)
            if subscription_snapshot is not None:
                self.lifecycle.restore_subscriptions(subscription_snapshot)
            else:
                self.lifecycle.unsubscribe(owner=f"plugin:{name}")
                self.container.unregister_plugin(name)
            raise PluginError(f"failed to install plugin {name!r}: {exc}") from exc

        candidate.consumed = True
        self._installed[name] = record
        await self.lifecycle.emit(
            LifecyclePoint.PLUGIN_INSTALLED,
            plugin=name,
            version=manifest.version,
            provides=list(manifest.provides),
            hooks=record.hooks,
            source=_record_source(record),
        )
        self.logger.info("installed plugin %s@%s", name, manifest.version)
        return record

    async def _cleanup_failed(
        self,
        record: InstalledPlugin,
        candidate: _Candidate,
    ) -> None:
        name = record.meta.name
        if record.facade is not None:
            record.facade.retire()
        self.lifecycle.unsubscribe(owner=f"plugin:{name}")
        for label, method in (
            ("destroy", record.plugin.on_destroy),
            ("uninstall", record.plugin.uninstall),
        ):
            try:
                await self._invoke(record, label, method, record.context)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                self.logger.exception("cleanup %s failed for plugin %s", label, name)
        self.container.unregister_plugin(name)
        candidate.dispose()

    async def _swap(
        self,
        name: str,
        candidate: _Candidate,
    ) -> InstalledPlugin:
        old = self._require(name)
        was_running = old.state == PluginState.RUNNING
        was_disabled = old.state == PluginState.DISABLED

        registry_snapshot = self.container.snapshot_state()
        subscription_snapshot = self.lifecycle.snapshot_subscriptions()
        stopped_old = False
        try:
            if was_running:
                await self._stop_record(old)
                stopped_old = True
            if old.facade is not None:
                old.facade.retire()
            self.lifecycle.unsubscribe(owner=f"plugin:{name}")
            self.container.unregister_plugin(name)
            self._installed.pop(name, None)
            activate = was_running and not was_disabled
            new_record = await self._activate_candidate(
                candidate,
                autostart=activate,
                use_snapshot=False,
            )
        except Exception as exc:
            self._installed.pop(name, None)
            self.container.restore_state(registry_snapshot)
            self.lifecycle.restore_subscriptions(subscription_snapshot)
            self._installed[name] = old
            if old.facade is not None:
                old.facade.reactivate()
            if was_running and stopped_old:
                try:
                    await self._start_record(old)
                except Exception as restart_exc:  # noqa: BLE001
                    old.state = PluginState.FAILED
                    old.metrics.record_error(restart_exc)
                    self.logger.exception("could not restart plugin %s after rollback", name)
            candidate.dispose()
            await self.lifecycle.emit(
                LifecyclePoint.PLUGIN_ROLLED_BACK,
                plugin=name,
                reason=repr(exc),
            )
            raise PluginError(f"failed to update plugin {name!r}: {exc}") from exc

        if was_disabled:
            self._withdraw_capabilities(new_record)
            new_record.state = PluginState.DISABLED

        # The new generation is active; retire the old one without touching the
        # registry (its facade is already inert).
        old.state = PluginState.DESTROYING
        for label, method in (
            ("destroy", old.plugin.on_destroy),
            ("uninstall", old.plugin.uninstall),
        ):
            try:
                await self._invoke(old, label, method, old.context)
            except Exception:  # noqa: BLE001 - old generation cleanup is best effort
                self.logger.exception("cleanup %s failed for retired plugin %s", label, name)
        old.state = PluginState.DESTROYED
        if old.sandbox is not None:
            old.sandbox.dispose()
        if old.staged is not None:
            old.staged.cleanup()

        await self.lifecycle.emit(
            LifecyclePoint.PLUGIN_RELOADED,
            plugin=name,
            version=new_record.meta.version,
            previous_version=old.meta.version,
        )
        self.logger.info(
            "reloaded plugin %s: %s -> %s",
            name,
            old.meta.version,
            new_record.meta.version,
        )
        return new_record

    async def _deactivate(self, name: str, *, emit: bool, force: bool) -> None:
        record = self._require(name)
        was_running = record.state == PluginState.RUNNING
        if was_running:
            try:
                await self._stop_record(record)
            except Exception:
                if not force:
                    await self._try_restart(record)
                    raise
                self.logger.exception("stop failed while unloading plugin %s", name)
        if record.facade is not None:
            record.facade.retire()
        self.lifecycle.unsubscribe(owner=f"plugin:{name}")
        self.container.unregister_plugin(name)
        record.state = PluginState.DESTROYING
        for label, method in (
            ("destroy", record.plugin.on_destroy),
            ("uninstall", record.plugin.uninstall),
        ):
            try:
                await self._invoke(record, label, method, record.context)
            except Exception:  # noqa: BLE001 - destructive cleanup is best effort
                self.logger.exception("cleanup %s failed for plugin %s", label, name)
        record.state = PluginState.DESTROYED
        if record.sandbox is not None:
            record.sandbox.dispose()
        if record.staged is not None:
            record.staged.cleanup()
        self._installed.pop(name, None)
        if emit:
            await self.lifecycle.emit(
                LifecyclePoint.PLUGIN_DESTROYED,
                plugin=name,
                version=record.meta.version,
            )
            await self.lifecycle.emit(
                LifecyclePoint.PLUGIN_UNINSTALLED,
                plugin=name,
                version=record.meta.version,
            )
        self.logger.info("uninstalled plugin %s", name)

    async def _start_record(self, record: InstalledPlugin) -> None:
        if record.state not in (
            PluginState.INITIALIZED,
            PluginState.STOPPED,
            PluginState.STARTING,
        ):
            raise PluginStateError(
                f"plugin {record.meta.name!r} cannot start from {record.state.value}"
            )
        record.state = PluginState.STARTING
        await self._invoke(record, "start", record.plugin.on_start, record.context)
        record.state = PluginState.RUNNING
        record.metrics.started_at = time.time()
        record.metrics.stopped_at = None
        await self.lifecycle.emit(
            LifecyclePoint.PLUGIN_STARTED,
            plugin=record.meta.name,
            version=record.meta.version,
        )

    async def _stop_record(self, record: InstalledPlugin) -> None:
        if record.state != PluginState.RUNNING:
            return
        record.state = PluginState.STOPPING
        try:
            await self._invoke(record, "stop", record.plugin.on_stop, record.context)
        except Exception:
            record.state = PluginState.RUNNING
            raise
        record.state = PluginState.STOPPED
        record.metrics.stopped_at = time.time()
        await self.lifecycle.emit(
            LifecyclePoint.PLUGIN_STOPPED,
            plugin=record.meta.name,
            version=record.meta.version,
        )

    async def _try_restart(self, record: InstalledPlugin) -> None:
        try:
            await self._start_record(record)
        except Exception as exc:  # noqa: BLE001
            record.metrics.record_error(exc)
            record.state = PluginState.FAILED
            self.logger.exception("rollback restart failed for plugin %s", record.meta.name)

    # ------------------------------------------------------------------
    # Lifecycle invocation, hooks and metrics
    # ------------------------------------------------------------------
    async def _invoke(
        self,
        record: InstalledPlugin,
        label: str,
        method: Any,
        *args: Any,
    ) -> Any:
        timeout = record.policy.max_hook_seconds if record.policy else None
        started = time.perf_counter()
        cpu_started = time.process_time()
        try:
            outcome = method(*args)
            if inspect.isawaitable(outcome):
                if timeout is not None:
                    return await asyncio.wait_for(outcome, timeout=timeout)
                return await outcome
            elapsed = time.perf_counter() - started
            if timeout is not None and elapsed > timeout:
                raise PluginSandboxError(
                    f"plugin {record.meta.name!r} stage {label!r} exceeded "
                    f"{timeout}s synchronous limit"
                )
            return outcome
        except asyncio.TimeoutError as exc:
            record.metrics.record_error(f"{label} timed out after {timeout}s")
            raise PluginSandboxError(
                f"plugin {record.meta.name!r} stage {label!r} timed out after {timeout}s"
            ) from exc
        except Exception as exc:
            record.metrics.record_error(exc)
            raise
        finally:
            record.metrics.add_stage(
                label,
                time.perf_counter() - started,
                time.process_time() - cpu_started,
            )

    def _subscribe_hooks(self, record: InstalledPlugin) -> None:
        hooks = record.plugin.hooks() or {}
        if not isinstance(hooks, Mapping):
            raise PluginValidationError(
                f"plugin {record.meta.name!r} hooks() must return a mapping"
            )
        owner = f"plugin:{record.meta.name}"
        for point, hook in hooks.items():
            self.lifecycle.subscribe(
                point,
                self._wrap_hook(record, hook),
                owner=owner,
            )
        record.hooks = len(hooks)

    def _wrap_hook(self, record: InstalledPlugin, hook: Hook) -> Hook:
        timeout = record.policy.max_hook_seconds if record.policy else None

        def handler(event: LifecycleEvent) -> Any:
            async def run() -> None:
                started = time.perf_counter()
                cpu_started = time.process_time()
                try:
                    outcome = hook(event)
                    if inspect.isawaitable(outcome):
                        if timeout is not None:
                            await asyncio.wait_for(outcome, timeout=timeout)
                        else:
                            await outcome
                except asyncio.TimeoutError as exc:
                    record.metrics.record_error(
                        f"hook {event.point.value} timed out after {timeout}s"
                    )
                    raise PluginSandboxError(
                        f"plugin {record.meta.name!r} hook {event.point.value!r} "
                        f"timed out after {timeout}s"
                    ) from exc
                except Exception as exc:
                    record.metrics.record_error(exc)
                    raise
                finally:
                    record.metrics.hook(
                        time.perf_counter() - started,
                        time.process_time() - cpu_started,
                    )

            return run()

        return handler

    # ------------------------------------------------------------------
    # Capability helpers
    # ------------------------------------------------------------------
    def _withdraw_capabilities(self, record: InstalledPlugin) -> None:
        name = record.meta.name
        entries: list[ComponentEntry[Any]] = []
        for slot in list(self.container):
            for entry in self.container.entries(slot):
                if entry.plugin == name:
                    entries.append(entry)
        defaults = tuple(
            (entry.slot, entry.name)
            for entry in entries
            if self.container.default_name(entry.slot) == entry.name
        )
        record.detached_entries = tuple(entries)
        record.detached_defaults = defaults
        if record.facade is not None:
            record.facade.retire()
        self.lifecycle.unsubscribe(owner=f"plugin:{name}")
        self.container.unregister_plugin(name)
        record.hooks = 0

    def _restore_capabilities(self, record: InstalledPlugin) -> None:
        name = record.meta.name
        for entry in record.detached_entries:
            self.container.register(
                entry.slot,
                entry.name,
                entry.component,
                plugin=name,
                priority=entry.priority,
                default=False,
                metadata=dict(entry.metadata),
            )
        for slot, component_name in record.detached_defaults:
            if self.container.contains(slot, component_name):
                self.container.set_default(slot, component_name)
        record.detached_entries = ()
        record.detached_defaults = ()

    def _capability_labels(self, record: InstalledPlugin) -> tuple[str, ...]:
        name = record.meta.name
        labels: list[str] = []
        for slot in list(self.container):
            for entry in self.container.entries(slot):
                if entry.plugin == name:
                    labels.append(f"{slot.value}:{entry.name}")
        if not labels:
            for entry in record.detached_entries:
                labels.append(f"{entry.slot.value}:{entry.name}")
        return tuple(labels)

    # ------------------------------------------------------------------
    # Candidate preparation
    # ------------------------------------------------------------------
    def _prepare_source(
        self,
        source: str | Path | PluginSource,
        config: Mapping[str, Any] | None,
    ) -> _Candidate:
        package_source = source_from(source)
        if package_source is None:
            if isinstance(source, str) and is_archive(source):
                raise PluginSourceError(f"plugin archive not found: {source}")
            reference = str(source)
            plugin = load_plugin(reference)
            return _Candidate(
                plugin=plugin,
                manifest=PluginManifest.from_meta(
                    plugin.meta, origin=f"reference:{reference}"
                ),
                config=dict(config or {}),
                reference=reference,
            )

        staged = package_source.stage()
        try:
            return self._candidate_from_staged(staged, config)
        except Exception:
            staged.cleanup()
            raise

    def _candidate_from_staged(
        self,
        staged: StagedPackage,
        config: Mapping[str, Any] | None,
    ) -> _Candidate:
        manifest = staged.manifest
        manifest.validate_host()
        if not manifest.entry:
            raise PluginValidationError(
                f"plugin package {manifest.name!r} has no entry module"
            )
        policy = sandbox_policy_from_manifest(
            manifest.sandbox, base=self.sandbox_policy
        )
        sandbox = PluginSandbox(manifest.name, staged.root, policy)
        try:
            plugin = sandbox.load_plugin(manifest.entry)
            if plugin.meta.name != manifest.name:
                raise PluginValidationError(
                    f"plugin class declares name {plugin.meta.name!r}, "
                    f"manifest declares {manifest.name!r}"
                )
            if plugin.meta.version != manifest.version:
                raise PluginValidationError(
                    f"plugin {manifest.name!r} version mismatch: manifest "
                    f"{manifest.version}, class {plugin.meta.version}"
                )
        except Exception:
            sandbox.dispose()
            raise
        return _Candidate(
            plugin=plugin,
            manifest=manifest,
            config=dict(config or {}),
            staged=staged,
            sandbox=sandbox,
            policy=policy,
        )

    def _candidate_from_record(self, record: InstalledPlugin) -> _Candidate:
        if record.reference:
            plugin = load_plugin(record.reference)
            return _Candidate(
                plugin=plugin,
                manifest=PluginManifest.from_meta(
                    plugin.meta, origin=f"reference:{record.reference}"
                ),
                config=dict(record.config),
                reference=record.reference,
            )
        if record.staged is not None:
            staged = _clone_staged(record.staged)
            try:
                candidate = self._candidate_from_staged(staged, record.config)
            except Exception:
                staged.cleanup()
                raise
            candidate.policy = record.policy
            return candidate
        try:
            plugin = type(record.plugin)()
        except Exception as exc:  # noqa: BLE001
            raise PluginStateError(
                f"plugin {record.meta.name!r} cannot be reloaded: no source descriptor "
                f"and its class cannot be instantiated ({exc})"
            ) from exc
        return _Candidate(
            plugin=plugin,
            manifest=PluginManifest.from_meta(plugin.meta, origin="in-memory"),
            config=dict(record.config),
        )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _ensure_absent(self, name: str) -> None:
        if name in self._installed:
            raise PluginError(f"plugin already installed: {name}")

    def _require(self, name: str) -> InstalledPlugin:
        record = self._installed.get(name)
        if record is None:
            raise PluginError(f"plugin not installed: {name}")
        return record

    def _ordered_names(self) -> list[str]:
        return [
            name
            for name, _ in sorted(
                self._installed.items(), key=lambda item: item[1].order
            )
        ]


def _has_manifest(path: Path) -> bool:
    return (path / "plugin.json").is_file() or (path / "manifest.json").is_file()


def _record_source(record: InstalledPlugin) -> str:
    if record.staged is not None:
        return record.staged.origin
    if record.reference:
        return f"reference:{record.reference}"
    return "in-memory"


def _clone_staged(staged: StagedPackage) -> StagedPackage:
    """Copy a staged package so reloading is independent of the old tempdir."""

    temporary = tempfile.TemporaryDirectory(prefix="chatanything-reload-")
    destination = Path(temporary.name) / "package"
    try:
        shutil.copytree(staged.root, destination)
        manifest_file = find_manifest(destination)
        if manifest_file is None:
            raise PluginValidationError(
                f"cannot reload {staged.manifest.name!r}: manifest missing in staged copy"
            )
        manifest = load_manifest(manifest_file)
    except Exception:
        temporary.cleanup()
        raise
    return StagedPackage(
        manifest=manifest,
        root=manifest_file.parent,
        origin=staged.origin,
        checksum=staged.checksum,
        temporary=temporary,
    )
