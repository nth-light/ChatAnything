"""HTTP UI and REST API event source plugin.

Installing this plugin makes the harness serve a lightweight web interface and
the REST API while it is running.  The server lifecycle follows the harness:
``Harness.start()`` calls :meth:`WebServicePlugin.start` and
``Harness.stop()`` calls :meth:`WebServicePlugin.stop`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from ...lifecycle import LifecycleEngine
from ...models import Trigger
from ...registry import Capability, ComponentRegistry
from ...webapp import HttpServer, WebApplication
from ..base import Plugin, PluginContext, PluginMeta


class WebServicePlugin(Plugin):
    meta = PluginMeta(
        name="web.http",
        version="0.1.0",
        description="Lightweight HTTP UI and REST API for ChatAnything.",
        provides=("event_source",),
        requires=("storage.sqlite", "tools.aicoding"),
        priority=30,
    )

    def __init__(self) -> None:
        self._container: ComponentRegistry | None = None
        self._lifecycle: LifecycleEngine | None = None
        self._host = "127.0.0.1"
        self._port = 8790
        self._request_timeout = 30.0
        self._static_dir = Path(__file__).resolve().parents[2] / "webapp" / "static"
        self._server: HttpServer | None = None

    async def install(self, context: PluginContext) -> None:
        self._container = context.container
        self._lifecycle = context.lifecycle
        self._host = str(context.setting("host", "127.0.0.1"))
        self._port = int(context.setting("port", 8790))
        static = context.setting("static_dir", None)
        self._static_dir = (
            Path(str(static))
            if static
            else Path(__file__).resolve().parents[2] / "webapp" / "static"
        )
        self._request_timeout = float(context.setting("request_timeout", 30.0))

        context.container.register(
            Capability.EVENT_SOURCE,
            "http",
            self,
            plugin=self.meta.name,
            priority=self.meta.priority,
            default=False,
        )

    async def start(self, emit: Callable[[Trigger], Awaitable[None]]) -> None:
        container = self._container
        lifecycle = self._lifecycle
        if container is None or lifecycle is None:
            raise RuntimeError("web.http plugin was not installed")
        loop = asyncio.get_running_loop()
        application = WebApplication(
            container,
            lifecycle,
            emit,
            loop,
            self._static_dir,
            request_timeout=self._request_timeout,
        )
        server = HttpServer(self._host, self._port, application)
        server.start()
        self._server = server

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.stop()

    @property
    def bound_port(self) -> int | None:
        server = self._server
        return server.bound_port if server is not None else None

    async def uninstall(self, context: PluginContext) -> None:
        await self.stop()


__all__ = ["WebServicePlugin"]
