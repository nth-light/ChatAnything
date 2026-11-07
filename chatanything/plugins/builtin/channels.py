"""Built-in communication channels (console + in-memory capture)."""

from __future__ import annotations

from ...models import OutboundMessage
from ...registry import Capability
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import Channel


class ConsoleChannel:
    """Print messages to stdout, mimicking a lightweight IM client."""

    def __init__(self, name: str = "console", *, prefix: str = "[ChatAnything]") -> None:
        self.name = name
        self.prefix = prefix

    async def send(self, message: OutboundMessage) -> None:
        header = f"{self.prefix} ({message.channel}/{message.kind})"
        if message.thread_id:
            header += f" thread={message.thread_id}"
        print(f"{header}\n{message.text}\n")


class MemoryChannel:
    """Capture outbound messages in memory (tests, notebooks, UI bridges)."""

    def __init__(self, name: str = "memory") -> None:
        self.name = name
        self.messages: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> None:
        self.messages.append(message)

    def clear(self) -> None:
        self.messages.clear()


class ConsoleChannelPlugin(Plugin):
    meta = PluginMeta(
        name="channel.console",
        version="0.1.0",
        description="Console and in-memory channels.",
        provides=("channel",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        prefix = str(context.setting("prefix", "[ChatAnything]"))
        context.container.register(
            Capability.CHANNEL,
            "console",
            ConsoleChannel(prefix=prefix),
            plugin=self.meta.name,
            priority=self.meta.priority,
        )
        context.container.register(
            Capability.CHANNEL,
            "memory",
            MemoryChannel(),
            plugin=self.meta.name,
            priority=self.meta.priority,
            default=False,
        )


_ = Channel
