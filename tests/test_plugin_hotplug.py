"""Hot-plug scenarios: lifecycle, isolation, dependencies, rollback, monitoring."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from chatanything.errors import (
    PluginDependencyCycleError,
    PluginDependencyError,
    PluginError,
    PluginLoadError,
    PluginSourceError,
    PluginValidationError,
    PluginVersionConflictError,
)
from chatanything.lifecycle import LifecycleEngine, LifecyclePoint
from chatanything.plugins.hotplug import PluginState, RemoteSource
from chatanything.plugins.hotplug.versioning import (
    Requirement,
    Version,
    parse_requirement,
)
from chatanything.plugins.manager import InstalledPlugin, PluginManager
from chatanything.registry import Capability, ComponentRegistry

PLUGIN_TEMPLATE = '''
from chatanything import Capability, Plugin, PluginMeta


class DemoPlugin(Plugin):
    meta = PluginMeta(
        name={name!r},
        version={version!r},
        requires={requires!r},
        provides=("llm",),
        priority={priority},
    )

    def __init__(self):
        self.events = []

    async def install(self, context):
        self.events.append("install")
        context.container.register(
            Capability.LLM,
            {component!r},
            self,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )
        {install_body}

    async def on_initialize(self, context):
        self.events.append("initialize")
        {initialize_body}

    async def on_start(self, context):
        self.events.append("start")
        {start_body}

    async def on_stop(self, context):
        self.events.append("stop")
        {stop_body}

    async def on_destroy(self, context):
        self.events.append("destroy")
        {destroy_body}

    async def uninstall(self, context):
        self.events.append("uninstall")


PLUGIN = DemoPlugin
'''


class VersionTestCase(unittest.TestCase):
    def test_zero_padding_and_compatibility(self):
        self.assertEqual(Version("1.0"), Version("1.0.0"))
        self.assertLess(Version("1.2"), Version("1.10"))
        self.assertTrue(parse_requirement("dep~=1.2").matches("1.2.9"))
        self.assertTrue(parse_requirement("dep~=1.2").matches("1.3.0"))
        self.assertFalse(parse_requirement("dep~=1.2").matches("2.0.0"))
        self.assertFalse(parse_requirement("dep~=1.2.3").matches("1.3.0"))
        self.assertTrue(Requirement("dep", ()).matches("42.0"))
        with self.assertRaises(PluginValidationError):
            Version("1.0-beta")


class HotPlugTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="chatanything-tests-")
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

        self.container = ComponentRegistry()
        self.lifecycle = LifecycleEngine(logging.getLogger("hotplug-tests"))
        self.manager = PluginManager(
            self.container,
            self.lifecycle,
            logger=logging.getLogger("hotplug-tests.manager"),
            resource_monitoring=True,
        )
        self.addAsyncCleanup(self.manager.shutdown)

    # ------------------------------------------------------------------
    # Package helpers
    # ------------------------------------------------------------------
    def _make_plugin(
        self,
        name: str,
        version: str = "1.0.0",
        *,
        requires: tuple[str, ...] = (),
        priority: int = 10,
        plugin_code: str | None = None,
        manifest_extra: dict | None = None,
        entry: str = "plugin.py:DemoPlugin",
    ) -> Path:
        package = self.root / name.replace(".", "_")
        package.mkdir(parents=True, exist_ok=True)
        if plugin_code is None:
            component = f"{name}.component"
            plugin_code = PLUGIN_TEMPLATE.format(
                name=name,
                version=version,
                requires=list(requires),
                priority=priority,
                component=component,
                install_body="",
                initialize_body="",
                start_body="",
                stop_body="",
                destroy_body="",
            )
        (package / "plugin.py").write_text(plugin_code, encoding="utf-8")
        manifest: dict = {
            "name": name,
            "version": version,
            "entry": entry,
            "provides": ["llm"],
            "priority": priority,
            "requires": list(requires),
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        (package / "plugin.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        return package

    def _plugin_with_failing_start(self, name: str) -> Path:
        return self._make_plugin(
            name,
            plugin_code=PLUGIN_TEMPLATE.format(
                name=name,
                version="1.0.0",
                requires=[],
                priority=10,
                component=f"{name}.component",
                install_body="",
                initialize_body="",
                start_body='raise RuntimeError("boom")',
                stop_body="",
                destroy_body="",
            ),
        )

    def _dialogue(self, record: InstalledPlugin | object) -> list[str]:
        plugin = record.plugin if isinstance(record, InstalledPlugin) else record
        return list(plugin.events)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Lifecycle / load / unload
    # ------------------------------------------------------------------
    async def test_lifecycle_hooks_load_and_unload(self):
        package = self._make_plugin("demo.a", version="1.1.0")
        record = await self.manager.load(package)

        self.assertEqual(record.state, PluginState.RUNNING)
        self.assertEqual(self._dialogue(record), ["install", "initialize", "start"])
        self.assertIs(
            self.container.resolve(Capability.LLM, "demo.a.component"), record.plugin
        )
        self.assertEqual(self.container.default_name(Capability.LLM), "demo.a.component")

        status = self.manager.status_of("demo.a")
        assert status is not None
        self.assertEqual(status.version, "1.1.0")
        self.assertTrue(status.running)
        self.assertEqual(status.capabilities, ("llm:demo.a.component",))
        self.assertGreaterEqual(status.metrics.cpu_seconds, 0.0)
        self.assertGreaterEqual(status.uptime_seconds, 0.0)
        self.assertIsNotNone(status.memory_bytes)
        self.assertIn("install", status.metrics.stage_seconds)
        self.assertIn("start", status.metrics.stage_seconds)

        prefix = record.sandbox.prefix if record.sandbox else ""

        await self.manager.unload("demo.a")
        self.assertEqual(
            self._dialogue(record),
            ["install", "initialize", "start", "stop", "destroy", "uninstall"],
        )
        self.assertIsNone(self.manager.get("demo.a"))
        self.assertFalse(self.container.contains(Capability.LLM, "demo.a.component"))
        self.assertFalse(
            [name for name in sys.modules if name.startswith(prefix + ".")]
        )

    async def test_repeated_load_is_rejected_without_corruption(self):
        package = self._make_plugin("demo.dup")
        first = await self.manager.load(package)
        with self.assertRaises(PluginError):
            await self.manager.load(package)
        self.assertEqual(self.manager.installed_names(), ("demo.dup",))
        self.assertEqual(len(self.container.names(Capability.LLM)), 1)
        self.assertIs(self.manager.get("demo.dup"), first)
        self.assertEqual(self._dialogue(first), ["install", "initialize", "start"])

    async def test_uninstall_is_idempotent_unload_is_not(self):
        await self.manager.uninstall("missing")
        with self.assertRaises(PluginError):
            await self.manager.unload("missing")

    # ------------------------------------------------------------------
    # Error containment / atomicity
    # ------------------------------------------------------------------
    async def test_invalid_packages_and_failing_start_are_contained(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(PluginSourceError):
            await self.manager.load(empty)

        bad_entry = self._make_plugin(
            "bad.entry", plugin_code="PLUGIN = object()\n"
        )
        with self.assertRaises(PluginLoadError):
            await self.manager.load(bad_entry)

        self.container.register(
            Capability.LLM, "base", object(), plugin="base", priority=5
        )
        self.assertEqual(self.container.default_name(Capability.LLM), "base")
        failing = self._plugin_with_failing_start("bad.start")

        subscribers_before = self.lifecycle.subscribers(LifecyclePoint.TRIGGER_INGESTED)
        with self.assertRaises(PluginError):
            await self.manager.load(failing)
        self.assertEqual(self.manager.installed_names(), ())
        self.assertFalse(self.container.contains(Capability.LLM, "bad.start.component"))
        self.assertEqual(self.container.default_name(Capability.LLM), "base")
        self.assertEqual(
            self.lifecycle.subscribers(LifecyclePoint.TRIGGER_INGESTED),
            subscribers_before,
        )
        self.assertEqual(self.container.names(Capability.LLM), ("base",))

    async def test_batch_install_rolls_back_on_second_failure(self):
        good = self._make_plugin("batch.good")
        bad = self._plugin_with_failing_start("batch.bad")
        with self.assertRaises(PluginError):
            await self.manager.load_many([good, bad])
        self.assertEqual(self.manager.installed_names(), ())
        self.assertEqual(self.container.names(Capability.LLM), ())
        self.assertEqual(self.lifecycle.subscribers(LifecyclePoint.TRIGGER_INGESTED), ())

    # ------------------------------------------------------------------
    # Enable / disable / reload / update
    # ------------------------------------------------------------------
    async def test_disable_enable_round_trip_restores_capability_and_default(self):
        package = self._make_plugin("toggle.a")
        record = await self.manager.load(package)
        component = "toggle.a.component"
        self.assertEqual(self.container.default_name(Capability.LLM), component)

        disabled = await self.manager.disable("toggle.a")
        self.assertEqual(disabled.state, PluginState.DISABLED)
        self.assertFalse(self.container.contains(Capability.LLM, component))
        self.assertIsNone(self.container.default_name(Capability.LLM))
        self.assertEqual(disabled.detached_entries[0].name, component)

        enabled = await self.manager.enable("toggle.a")
        self.assertEqual(enabled.state, PluginState.RUNNING)
        self.assertTrue(self.container.contains(Capability.LLM, component))
        self.assertEqual(self.container.default_name(Capability.LLM), component)
        self.assertEqual(
            self._dialogue(record),
            ["install", "initialize", "start", "stop", "start"],
        )
        self.assertIs(
            self.container.resolve(Capability.LLM, component), record.plugin
        )

    async def test_reload_replaces_instance_and_generation(self):
        package = self._make_plugin("reload.a")
        old_record = await self.manager.load(package)
        old_plugin = old_record.plugin
        old_generation = old_record.generation

        new_record = await self.manager.reload("reload.a")
        self.assertIsNot(new_record.plugin, old_plugin)
        self.assertGreater(new_record.generation, old_generation)
        self.assertEqual(self._dialogue(old_plugin), ["install", "initialize", "start", "stop", "destroy", "uninstall"])
        self.assertEqual(self._dialogue(new_record), ["install", "initialize", "start"])
        self.assertIs(
            self.container.resolve(Capability.LLM, "reload.a.component"),
            new_record.plugin,
        )
        self.assertEqual(self.manager.status_of("reload.a").version, "1.0.0")  # type: ignore[union-attr]

    async def test_update_version_and_rollback_on_bad_start(self):
        first = self._make_plugin("update.a", version="1.0.0")
        await self.manager.load(first)
        second = self._make_plugin("update.a", version="2.0.0")
        updated = await self.manager.update("update.a", second)
        self.assertEqual(updated.meta.version, "2.0.0")
        self.assertEqual(self.manager.status_of("update.a").version, "2.0.0")  # type: ignore[union-attr]

        third = self._make_plugin(
            "update.a",
            version="3.0.0",
            plugin_code=PLUGIN_TEMPLATE.format(
                name="update.a",
                version="3.0.0",
                requires=[],
                priority=10,
                component="update.a.component",
                install_body="",
                initialize_body="",
                start_body='raise RuntimeError("bad v3")',
                stop_body="",
                destroy_body="",
            ),
        )
        with self.assertRaises(PluginError):
            await self.manager.update("update.a", third)

        status = self.manager.status_of("update.a")
        assert status is not None
        self.assertEqual(status.version, "2.0.0")
        self.assertEqual(status.state, PluginState.RUNNING)
        self.assertIs(
            self.container.resolve(Capability.LLM, "update.a.component"),
            updated.plugin,
        )
        self.assertEqual(
            self._dialogue(updated.plugin),
            ["install", "initialize", "start", "stop", "start"],
        )

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------
    async def test_dependency_order_and_cycle_detection(self):
        dep = self._make_plugin("dep.a", version="1.2.0")
        consumer = self._make_plugin(
            "consumer.b", requires=("dep.a>=1.0,<2.0",)
        )
        records = await self.manager.load_many([consumer, dep])
        self.assertEqual([record.meta.name for record in records], ["dep.a", "consumer.b"])
        self.assertLess(
            self.manager.get("dep.a").order, self.manager.get("consumer.b").order  # type: ignore[union-attr]
        )

        await self.manager.shutdown()
        cyclic_a = self._make_plugin("cycle.a", requires=("cycle.b",))
        cyclic_b = self._make_plugin("cycle.b", requires=("cycle.a",))
        with self.assertRaises(PluginDependencyCycleError):
            await self.manager.load_many([cyclic_a, cyclic_b])
        self.assertEqual(self.manager.installed_names(), ())

    async def test_missing_dependency_and_version_conflict(self):
        dep = self._make_plugin("dep.conflict", version="1.0.0")
        await self.manager.load(dep)

        too_new = self._make_plugin(
            "needs.new", requires=("dep.conflict>=2.0",)
        )
        with self.assertRaises(PluginVersionConflictError):
            await self.manager.load(too_new)
        self.assertNotIn("needs.new", self.manager.installed_names())

        missing = self._make_plugin("needs.missing", requires=("ghost.plugin",))
        with self.assertRaises(PluginDependencyError):
            await self.manager.load(missing)

    # ------------------------------------------------------------------
    # Sources: archive / remote / directory
    # ------------------------------------------------------------------
    async def test_archive_and_remote_sources_with_checksum(self):
        package = self._make_plugin("remote.a")
        archive = self.root / "remote.a.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for file in sorted(package.rglob("*")):
                if file.is_file():
                    bundle.write(file, Path(package.name) / file.relative_to(package))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()

        invalid = RemoteSource(archive.as_uri(), checksum="0" * 64)
        with self.assertRaises(PluginSourceError):
            await self.manager.load(invalid)

        loaded = await self.manager.load(
            RemoteSource(archive.as_uri(), checksum=f"sha256:{digest}")
        )
        self.assertEqual(loaded.meta.name, "remote.a")
        self.assertTrue(self.container.contains(Capability.LLM, "remote.a.component"))

    async def test_load_directory_scans_child_packages(self):
        parent = self.root / "plugins-root"
        parent.mkdir()
        for name in ("scan.a", "scan.b"):
            package = self._make_plugin(name)
            shutil.move(str(package), str(parent / package.name))
        records = await self.manager.load_directory(parent)
        self.assertEqual({record.meta.name for record in records}, {"scan.a", "scan.b"})

    # ------------------------------------------------------------------
    # Sandbox isolation
    # ------------------------------------------------------------------
    async def test_sandbox_blocks_denied_imports(self):
        denied = self._make_plugin(
            "sandbox.denied",
            plugin_code="import subprocess\nPLUGIN = object()\n",
            manifest_extra={"sandbox": {"allow_subprocess": False}},
        )
        with self.assertRaises(PluginLoadError):
            await self.manager.load(denied)
        self.assertEqual(self.manager.installed_names(), ())

        allowed_code = PLUGIN_TEMPLATE.format(
            name="sandbox.allowed",
            version="1.0.0",
            requires=[],
            priority=10,
            component="sandbox.allowed.component",
            install_body="",
            initialize_body="",
            start_body="",
            stop_body="",
            destroy_body="",
        ).replace(
            "from chatanything import Capability, Plugin, PluginMeta",
            "import subprocess\nfrom chatanything import Capability, Plugin, PluginMeta",
        )
        allowed = self._make_plugin(
            "sandbox.allowed",
            plugin_code=allowed_code,
            manifest_extra={"sandbox": {"allow_subprocess": True}},
        )
        record = await self.manager.load(allowed)
        self.assertEqual(record.state, PluginState.RUNNING)

    async def test_module_namespaces_do_not_collide(self):
        code = '''
from chatanything import Capability, Plugin, PluginMeta
from . import helper


class DemoPlugin(Plugin):
    meta = PluginMeta(name={name!r}, version="1.0.0")

    async def install(self, context):
        context.container.register(
            Capability.LLM, {component!r}, helper.VALUE, plugin=self.meta.name
        )


PLUGIN = DemoPlugin
'''
        packages = {}
        for token in ("A", "B"):
            name = f"isolation.{token.lower()}"
            package = self._make_plugin(
                name,
                plugin_code=code.format(
                    name=name, component=f"{name}.component"
                ),
            )
            (package / "helper.py").write_text(
                f'VALUE = "{token}"\n', encoding="utf-8"
            )
            packages[token] = package

        await self.manager.load_many(list(packages.values()))
        self.assertEqual(
            self.container.resolve(Capability.LLM, "isolation.a.component"), "A"
        )
        self.assertEqual(
            self.container.resolve(Capability.LLM, "isolation.b.component"), "B"
        )

    # ------------------------------------------------------------------
    # Concurrency and monitoring
    # ------------------------------------------------------------------
    async def test_concurrent_load_of_same_plugin_is_consistent(self):
        package = self._make_plugin("concurrent.a")
        results = await asyncio.gather(
            *[self.manager.load(package) for _ in range(5)],
            return_exceptions=True,
        )
        successes = [item for item in results if isinstance(item, InstalledPlugin)]
        failures = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 4)
        self.assertTrue(all(isinstance(item, PluginError) for item in failures))
        self.assertEqual(self.manager.installed_names(), ("concurrent.a",))
        self.assertEqual(self.container.names(Capability.LLM), ("concurrent.a.component",))
        self.assertEqual(self._dialogue(successes[0]), ["install", "initialize", "start"])

    async def test_status_reports_resources_and_hook_errors(self):
        package = self._make_plugin(
            "monitor.a",
            plugin_code=PLUGIN_TEMPLATE.format(
                name="monitor.a",
                version="1.0.0",
                requires=[],
                priority=10,
                component="monitor.a.component",
                install_body="",
                initialize_body="",
                start_body="",
                stop_body="",
                destroy_body="",
            ).replace(
                "PLUGIN = DemoPlugin",
                "def broken_hook(event):\n"
                "    raise RuntimeError('hook boom')\n"
                "\n"
                "DemoPlugin.hooks = lambda self: {LifecyclePoint.TRIGGER_INGESTED: broken_hook}\n"
                "PLUGIN = DemoPlugin",
            ).replace(
                "from chatanything import Capability, Plugin, PluginMeta",
                "from chatanything import Capability, LifecyclePoint, Plugin, PluginMeta",
            ),
        )
        record = await self.manager.load(package)
        await self.lifecycle.emit(LifecyclePoint.TRIGGER_INGESTED)

        status = self.manager.status_of("monitor.a")
        assert status is not None
        self.assertGreaterEqual(status.metrics.hook_invocations, 1)
        self.assertGreaterEqual(status.metrics.errors, 1)
        self.assertIn("hook boom", status.metrics.last_error)
        self.assertIsNotNone(status.memory_bytes)
        self.assertEqual(self.manager.process_usage()["cpu_seconds"] >= 0.0, True)
        self.assertEqual(record.state, PluginState.RUNNING)
