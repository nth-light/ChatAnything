"""Optional speculative-execution policy (extension point)."""

from __future__ import annotations

from ...models import Speculation, Trigger
from ...registry import Capability
from ...runtime import Runtime
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import Speculator


class NoopSpeculator:
    """Default policy: never speculate.

    A real implementation can start background work in ``prepare`` and tell
    the Router (via ``evaluate``) whether the tentative result can be adopted.
    """

    name = "noop"

    async def prepare(self, trigger: Trigger, runtime: Runtime) -> Speculation | None:
        return None

    async def evaluate(
        self,
        trigger: Trigger,
        speculation: Speculation,
        runtime: Runtime,
    ) -> bool:
        return False


class NoopSpeculatorPlugin(Plugin):
    meta = PluginMeta(
        name="speculator.noop",
        version="0.1.0",
        description="No-op speculative execution policy.",
        provides=("speculator",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        context.container.register(
            Capability.SPECULATOR,
            "noop",
            NoopSpeculator(),
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


_ = Speculator
