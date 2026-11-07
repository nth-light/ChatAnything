"""Optional proactive event source (scheduler driven)."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from ...models import Trigger
from ...registry import Capability
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import EventSource

DEFAULT_TICK_TEXT = "[periodic check] Review ongoing work and report only if action is needed."


class IntervalEventSource:
    """Emit a trigger every N seconds -- a minimal proactive heartbeat."""

    name = "interval"

    def __init__(
        self,
        *,
        interval_seconds: float = 30.0,
        text: str = DEFAULT_TICK_TEXT,
        source: str = "clock",
        channel: str = "console",
    ) -> None:
        self.interval_seconds = interval_seconds
        self.text = text
        self.source = source
        self.channel = channel
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self, emit: Callable[[Trigger], Awaitable[None]]) -> None:
        self._stop.clear()

        async def loop() -> None:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    await emit(
                        Trigger(
                            text=self.text,
                            source=self.source,
                            channel=self.channel,
                            event_type="tick",
                            actor="scheduler",
                            metadata={"scheduled": True, "interval": self.interval_seconds},
                        )
                    )

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


class IntervalSourcePlugin(Plugin):
    """Not installed by default; add it to ``plugin_modules`` to enable."""

    meta = PluginMeta(
        name="source.interval",
        version="0.1.0",
        description="Scheduler-driven proactive trigger source.",
        provides=("event_source",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        source = IntervalEventSource(
            interval_seconds=float(context.setting("interval_seconds", 30.0)),
            text=str(context.setting("text", DEFAULT_TICK_TEXT)),
            source=str(context.setting("source", "clock")),
            channel=str(context.setting("channel", "console")),
        )
        context.container.register(
            Capability.EVENT_SOURCE,
            source.name,
            source,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


_ = EventSource
