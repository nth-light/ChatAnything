"""Plugin convention checks: metadata, registry wiring, cleanup and boundaries."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import chatanything as package
from chatanything.config import HarnessConfig
from chatanything.core.harness import Harness
from chatanything.models import ModelConfig
from chatanything.registry import Capability
from chatanything.storage import SqliteDatabase

STORAGE_PLUGIN = "chatanything.plugins.builtin.storage_sqlite:SqliteStoragePlugin"
TOOLS_PLUGIN = "chatanything.plugins.builtin.aicoding_tools:AICodingToolsPlugin"

_AI_CODING_TOOLS = ("web_search", "file_edit", "run_command")


class PluginConventionTestCase(unittest.IsolatedAsyncioTestCase):
    async def _make_harness(
        self,
        plugin_modules: list[str],
        plugin_config: dict[str, dict[str, object]],
    ) -> Harness:
        harness = Harness(
            HarnessConfig(
                log_level=40,
                plugin_modules=plugin_modules,
                plugin_config=plugin_config,
            )
        )
        await harness.setup()
        self.addAsyncCleanup(harness.stop)
        return harness

    async def test_plugin_metadata_registry_wiring_and_cleanup(self):
        harness = await self._make_harness(
            [STORAGE_PLUGIN, TOOLS_PLUGIN],
            {
                "storage.sqlite": {"path": ":memory:"},
                "tools.aicoding": {"root": "."},
            },
        )

        storage = harness.plugins.get("storage.sqlite")
        self.assertIsNotNone(storage)
        self.assertEqual(storage.meta.name, "storage.sqlite")
        self.assertEqual(
            storage.meta.provides, ("memory", "config_store", "mcp", "skill")
        )

        tools = harness.plugins.get("tools.aicoding")
        self.assertIsNotNone(tools)
        self.assertEqual(tools.meta.name, "tools.aicoding")
        self.assertEqual(tools.meta.provides, ("tool",))

        snapshot = harness.container.snapshot()
        self.assertIn("in_memory", snapshot["memory"])
        self.assertIn("sqlite", snapshot["memory"])
        self.assertEqual(harness.runtime.memory.name, "sqlite")
        for slot in ("config_store", "mcp", "skill"):
            self.assertIn("sqlite", snapshot[slot])
        self.assertEqual(harness.container.default_name(Capability.MEMORY), "sqlite")

        entries = {
            entry.name: entry.component
            for entry in harness.container.entries(Capability.TOOL)
        }
        for name in _AI_CODING_TOOLS:
            self.assertIn(name, entries)
            self.assertEqual(entries[name].spec.name, name)

        await harness.stop()
        self.assertFalse(harness.container.contains(Capability.MEMORY, "sqlite"))
        self.assertFalse(harness.container.contains(Capability.MEMORY, "in_memory"))

    async def test_storage_configuration_comes_from_plugin_context(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        database_path = os.path.join(tmp.name, "configured.db")

        harness = await self._make_harness(
            [STORAGE_PLUGIN],
            {"storage.sqlite": {"path": database_path}},
        )
        store = harness.container.resolve(Capability.CONFIG_STORE)
        saved = await store.save_model_config(
            ModelConfig(name="ctx-model", provider="openai", model="gpt-4o")
        )
        self.assertEqual(saved.name, "ctx-model")
        await harness.stop()

        # The configured path (not a default path) holds the data.
        database = SqliteDatabase(database_path)
        database.initialize()
        try:
            persisted = await database.model_configs.get_model_config(saved.id)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.name, "ctx-model")
        finally:
            database.close()

    async def test_default_harness_keeps_in_memory_store(self):
        harness = Harness(HarnessConfig(log_level=40))
        await harness.setup()
        self.addAsyncCleanup(harness.stop)

        self.assertEqual(harness.runtime.memory.name, "in_memory")
        self.assertNotIn(
            "sqlite", harness.container.snapshot().get("memory", ())
        )
        await harness.stop()

    def test_core_does_not_import_concrete_plugins(self):
        package_root = Path(package.__file__).resolve().parent
        sources = [
            package_root / "registry.py",
            package_root / "models.py",
            package_root / "plugins" / "interfaces.py",
        ] + sorted((package_root / "core").glob("*.py"))

        for path in sources:
            text = path.read_text(encoding="utf-8")
            for token in ("storage_sqlite", "aicoding_tools", "web_service"):
                self.assertNotIn(token, text, f"{path} must not reference {token}")


if __name__ == "__main__":
    unittest.main()
