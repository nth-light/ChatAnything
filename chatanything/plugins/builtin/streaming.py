"""In-memory stream hub: the default ``stream`` capability.

The hub decouples producers (LLM / agent loops, running on the asyncio loop)
from consumers (SSE connections, running in HTTP threads).  It therefore uses
plain :class:`queue.Queue` objects instead of asyncio primitives: every method
is synchronous and thread-safe.

Each stream is keyed by a string such as ``trigger:<trigger_id>``.  Recent
events are buffered briefly so a browser that connects a few milliseconds
after the message was accepted still receives the beginning of the answer.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Mapping

from ...registry import Capability
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import StreamBroker, StreamSubscription

_TERMINAL_EVENTS = frozenset({"done", "error"})


class _Buffer:
    """Buffered events plus housekeeping metadata for one stream key."""

    __slots__ = ("events", "updated_at")

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.updated_at = time.time()


class _Subscription:
    """Blocking event queue handed to one SSE connection."""

    __slots__ = ("key", "_queue", "_hub", "_closed")

    def __init__(self, hub: "InMemoryStreamHub", key: str) -> None:
        self.key = key
        self._hub = hub
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._closed = False

    def feed(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        self._queue.put(event)

    def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("stream is idle") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._hub._detach(self)


class InMemoryStreamHub:
    """Thread-safe fan-out hub with bounded per-key replay buffers."""

    name = "memory"

    def __init__(
        self,
        *,
        max_events_per_key: int = 2048,
        max_keys: int = 256,
        ttl_seconds: float = 600.0,
    ) -> None:
        self.max_events_per_key = max(1, int(max_events_per_key))
        self.max_keys = max(1, int(max_keys))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._lock = threading.Lock()
        self._buffers: dict[str, _Buffer] = {}
        self._subscribers: dict[str, set[_Subscription]] = {}

    # ------------------------------------------------------------------
    # StreamBroker protocol
    # ------------------------------------------------------------------
    def publish(self, key: str, event: Mapping[str, Any]) -> None:
        payload = dict(event)
        with self._lock:
            now = time.time()
            self._prune(now)
            buffer = self._buffers.get(key)
            if buffer is None:
                buffer = _Buffer()
                if len(self._buffers) >= self.max_keys:
                    oldest_key = min(
                        self._buffers, key=lambda item: self._buffers[item].updated_at
                    )
                    self._buffers.pop(oldest_key, None)
                self._buffers[key] = buffer
            buffer.events.append(payload)
            if len(buffer.events) > self.max_events_per_key:
                del buffer.events[: len(buffer.events) - self.max_events_per_key]
            buffer.updated_at = now
            subscribers = list(self._subscribers.get(key, ()))

        for subscription in subscribers:
            subscription.feed(payload)

    def subscribe(self, key: str, *, replay: bool = True) -> StreamSubscription:
        subscription = _Subscription(self, key)
        with self._lock:
            self._prune(time.time())
            if replay:
                buffer = self._buffers.get(key)
                if buffer is not None:
                    for event in buffer.events:
                        subscription.feed(event)
            self._subscribers.setdefault(key, set()).add(subscription)
        return subscription

    # ------------------------------------------------------------------
    # Introspection / lifecycle
    # ------------------------------------------------------------------
    def active_streams(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._subscribers)

    def close(self) -> None:
        """Drop every buffer and subscriber (used on shutdown)."""

        with self._lock:
            subscribers = [
                item for bucket in self._subscribers.values() for item in bucket
            ]
            self._subscribers.clear()
            self._buffers.clear()
        for subscription in subscribers:
            subscription._closed = True

    def _detach(self, subscription: _Subscription) -> None:
        with self._lock:
            bucket = self._subscribers.get(subscription.key)
            if bucket is None:
                return
            bucket.discard(subscription)
            if not bucket:
                self._subscribers.pop(subscription.key, None)

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, buffer in self._buffers.items()
            if now - buffer.updated_at > self.ttl_seconds
        ]
        for key in expired:
            self._buffers.pop(key, None)


class StreamingHubPlugin(Plugin):
    meta = PluginMeta(
        name="stream.memory",
        version="0.1.0",
        description="In-memory stream hub used for token-level SSE output.",
        provides=("stream",),
        priority=0,
    )

    def __init__(self) -> None:
        self._hub = InMemoryStreamHub()

    async def install(self, context: PluginContext) -> None:
        self._hub = InMemoryStreamHub(
            max_events_per_key=int(context.setting("max_events_per_key", 2048)),
            max_keys=int(context.setting("max_keys", 256)),
            ttl_seconds=float(context.setting("ttl_seconds", 600.0)),
        )
        context.container.register(
            Capability.STREAM,
            self._hub.name,
            self._hub,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )

    async def uninstall(self, context: PluginContext) -> None:
        self._hub.close()

    @property
    def hub(self) -> InMemoryStreamHub:
        return self._hub


_ = StreamBroker
_ = StreamSubscription

__all__ = ["InMemoryStreamHub", "StreamingHubPlugin"]
