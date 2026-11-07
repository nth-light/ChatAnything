"""Runtime services handed to policies, loops, tools and filters."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import HarnessConfig
from .lifecycle import LifecycleEngine, LifecyclePoint
from .models import OutboundMessage
from .plugins.interfaces import Channel, MemoryStore
from .registry import Capability, ComponentRegistry


class ChannelRouter:
    """Deliver outbound messages to the right multi-channel plugin."""

    def __init__(
        self,
        container: ComponentRegistry,
        *,
        default_channel: str = "console",
        logger: logging.Logger | None = None,
    ) -> None:
        self.container = container
        self.default_channel = default_channel
        self.logger = logger or logging.getLogger("chatanything.channels")

    async def deliver(self, message: OutboundMessage) -> str:
        """Send a message; falls back to the default channel. Returns channel name."""

        channel_name = message.channel or self.default_channel
        try:
            channel = self.container.resolve(Capability.CHANNEL, channel_name)
        except Exception:
            channel = self.container.resolve(Capability.CHANNEL, self.default_channel)
            channel_name = self.default_channel
        await channel.send(message)
        return channel_name


@dataclass(slots=True)
class Runtime:
    """A stable service surface shared by all runtime components."""

    container: ComponentRegistry
    lifecycle: LifecycleEngine
    config: HarnessConfig
    logger: logging.Logger
    memory: MemoryStore
    channels: ChannelRouter

    def resolve(self, slot: Capability, name: str | None = None) -> object:
        return self.container.resolve(slot, name)

    def resolve_all(self, slot: Capability) -> tuple[object, ...]:
        return self.container.resolve_all(slot)

    async def emit(self, point: LifecyclePoint, **payload: object) -> None:
        await self.lifecycle.emit(point, **payload)

    async def deliver(self, message: OutboundMessage) -> str:
        channel_name = await self.channels.deliver(message)
        await self.lifecycle.emit(
            LifecyclePoint.CHANNEL_DELIVERED,
            channel=channel_name,
            message=message,
        )
        return channel_name
