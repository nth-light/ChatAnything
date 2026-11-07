"""SQLite-backed storage plugin.

Registers ``memory`` / ``config_store`` / ``mcp`` / ``skill`` components that
share a single SQLite database file.  Installation is explicit: add
``chatanything.plugins.builtin.storage_sqlite:SqliteStoragePlugin`` to
``HarnessConfig.plugin_modules``.  Because ``memory`` is registered with
``default=True`` it replaces the built-in ``in_memory`` default for that slot.
"""

from __future__ import annotations

from ...registry import Capability
from ...storage import SqliteDatabase
from ..base import Plugin, PluginContext, PluginMeta


class SqliteStoragePlugin(Plugin):
    meta = PluginMeta(
        name="storage.sqlite",
        version="0.1.0",
        description="SQLite-backed memory, model config, MCP and skill stores.",
        provides=("memory", "config_store", "mcp", "skill"),
        priority=20,
    )

    _db: SqliteDatabase | None = None

    async def install(self, context: PluginContext) -> None:
        path = str(context.setting("path", "./chatanything.db"))
        busy_timeout = float(context.setting("busy_timeout", 5.0))
        journal_mode = str(context.setting("journal_mode", "WAL"))
        if self._db is not None:
            self._db.close()
        db = SqliteDatabase(
            path,
            busy_timeout=busy_timeout,
            journal_mode=journal_mode,
        )
        db.initialize()
        context.container.register(
            Capability.MEMORY,
            "sqlite",
            db.memory,
            plugin=self.meta.name,
            priority=self.meta.priority,
            default=True,
        )
        context.container.register(
            Capability.CONFIG_STORE,
            "sqlite",
            db.model_configs,
            plugin=self.meta.name,
            priority=self.meta.priority,
            default=True,
        )
        context.container.register(
            Capability.MCP,
            "sqlite",
            db.mcp,
            plugin=self.meta.name,
            priority=self.meta.priority,
            default=True,
        )
        context.container.register(
            Capability.SKILL,
            "sqlite",
            db.skills,
            plugin=self.meta.name,
            priority=self.meta.priority,
            default=True,
        )
        self._db = db

    async def uninstall(self, context: PluginContext) -> None:
        db = self._db
        if db is not None:
            db.close()
        self._db = None
