"""ChatAnything quickstart.

Run::

    python examples/quickstart.py

The default stack is offline and deterministic (EchoLLM + heuristic Router
fallback), so no API key is required.  Replace the LLM plugin for real use.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Allow running the example directly from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatanything import Harness, HarnessConfig, LifecyclePoint, Plugin, PluginMeta


class TracePlugin(Plugin):
    """A tiny observability plugin: print a lifecycle trace."""

    meta = PluginMeta(
        name="example.trace",
        version="0.1.0",
        description="Print selected lifecycle points to the console.",
        priority=20,
    )

    async def install(self, context) -> None:
        for point in (
            LifecyclePoint.TRIGGER_INGESTED,
            LifecyclePoint.TRIGGER_FILTERED,
            LifecyclePoint.TRIGGER_ROUTED,
            LifecyclePoint.SESSION_CREATED,
            LifecyclePoint.TOOL_CALL_STARTED,
            LifecyclePoint.SESSION_REPORTED,
        ):
            context.lifecycle.subscribe(point, self._trace, owner=self.meta.name)

    async def _trace(self, event) -> None:
        trigger = event.payload.get("trigger")
        extra = trigger.brief(60) if trigger else ""
        session = event.payload.get("session")
        if session is not None:
            extra = f"{session.kind.value}:{session.id}"
        print(f"  · lifecycle {event.point.value} {extra}")


async def main() -> None:
    async with Harness(HarnessConfig(log_level=logging.WARNING)) as harness:
        # Plugins may be installed before or after setup; this one is installed
        # at runtime without touching any framework source.
        await harness.plugins.install(TracePlugin())

        await harness.submit(
            "请帮我确认一下这个方案是否可行？如果可行，说明主要风险。",
            actor="alice",
            thread_id="thread-demo",
        )
        await harness.wait_until_idle(timeout=15)

        print("\nHarness state:", harness.state.value)
        print("Plugins:", ", ".join(harness.plugins.installed_names()))
        workers = await harness.runtime.memory.list_sessions(kind="worker")
        print("Worker sessions:", [session.id for session in workers])


if __name__ == "__main__":
    asyncio.run(main())
