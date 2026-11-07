"""Lifecycle engine: the heartbeat of the harness.

Every meaningful action inside ChatAnything is expressed as a
:class:`LifecyclePoint`.  Plugins never patch the core; they subscribe to
points and contribute capabilities.  This is what "lifecycle-driven" means in
practice:

* the harness owns a validated state machine (created -> ready -> running -> ...);
* runtime steps emit ordered, observable lifecycle events;
* plugins can observe, log, audit, mutate state or trigger side effects at any
  point without touching framework internals.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Mapping


class LifecycleState(str, Enum):
    """High-level state of a harness instance."""

    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class LifecyclePoint(str, Enum):
    """Fine-grained lifecycle points emitted by the runtime and plugins."""

    # Harness lifecycle
    HARNESS_SETUP = "harness_setup"
    HARNESS_READY = "harness_ready"
    HARNESS_STARTED = "harness_started"
    HARNESS_TICK = "harness_tick"
    HARNESS_PAUSED = "harness_paused"
    HARNESS_RESUMED = "harness_resumed"
    HARNESS_STOPPING = "harness_stopping"
    HARNESS_STOPPED = "harness_stopped"
    HARNESS_ERROR = "harness_error"

    # Plugin lifecycle
    PLUGIN_VALIDATED = "plugin_validated"
    PLUGIN_INSTALLED = "plugin_installed"
    PLUGIN_INITIALIZED = "plugin_initialized"
    PLUGIN_STARTED = "plugin_started"
    PLUGIN_STOPPED = "plugin_stopped"
    PLUGIN_DESTROYED = "plugin_destroyed"
    PLUGIN_DISABLED = "plugin_disabled"
    PLUGIN_ENABLED = "plugin_enabled"
    PLUGIN_RELOADED = "plugin_reloaded"
    PLUGIN_ROLLED_BACK = "plugin_rolled_back"
    PLUGIN_UNINSTALLED = "plugin_uninstalled"

    # Trigger / event lifecycle
    TRIGGER_INGESTED = "trigger_ingested"
    TRIGGER_FILTERED = "trigger_filtered"
    TRIGGER_ROUTED = "trigger_routed"
    TRIGGER_IGNORED = "trigger_ignored"

    # Session lifecycle
    SESSION_CREATED = "session_created"
    SESSION_STARTED = "session_started"
    SESSION_REPORTED = "session_reported"
    SESSION_CLOSED = "session_closed"
    SESSION_FAILED = "session_failed"

    # Context / model lifecycle
    CONTEXT_BUILDING = "context_building"
    CONTEXT_BUILT = "context_built"
    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_CALL = "after_model_call"
    OUTPUT_PARSED = "output_parsed"

    # Tool lifecycle
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"

    # Interaction lifecycle
    REPORT_RECEIVED = "report_received"
    CHANNEL_DELIVERED = "channel_delivered"

    # External control loop
    WATCHDOG_CHECKED = "watchdog_checked"
    WATCHDOG_RECOVERED = "watchdog_recovered"

    # Speculative execution extension points
    SPECULATION_PREPARED = "speculation_prepared"
    SPECULATION_ADOPTED = "speculation_adopted"
    SPECULATION_DISCARDED = "speculation_discarded"


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """An immutable record passed to every lifecycle hook."""

    point: LifecyclePoint
    timestamp: float
    state: LifecycleState
    payload: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


Hook = Callable[[LifecycleEvent], Awaitable[None] | None]


@dataclass(slots=True)
class _Subscription:
    point: LifecyclePoint
    handler: Hook
    owner: str
    priority: int
    sequence: int
    once: bool = False


_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.CREATED: frozenset({LifecycleState.INITIALIZING, LifecycleState.FAILED}),
    LifecycleState.INITIALIZING: frozenset({LifecycleState.READY, LifecycleState.FAILED}),
    LifecycleState.READY: frozenset(
        {LifecycleState.RUNNING, LifecycleState.STOPPING, LifecycleState.FAILED}
    ),
    LifecycleState.RUNNING: frozenset(
        {LifecycleState.PAUSED, LifecycleState.STOPPING, LifecycleState.FAILED}
    ),
    LifecycleState.PAUSED: frozenset(
        {LifecycleState.RUNNING, LifecycleState.STOPPING, LifecycleState.FAILED}
    ),
    LifecycleState.STOPPING: frozenset({LifecycleState.STOPPED, LifecycleState.FAILED}),
    LifecycleState.STOPPED: frozenset(),
    LifecycleState.FAILED: frozenset({LifecycleState.STOPPING, LifecycleState.STOPPED}),
}


class LifecycleEngine:
    """A synchronous/async hook dispatcher plus a validated state machine."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        raise_on_hook_error: bool = False,
        history_size: int = 500,
    ) -> None:
        self.logger = logger or logging.getLogger("chatanything.lifecycle")
        self.raise_on_hook_error = raise_on_hook_error
        self.state = LifecycleState.CREATED
        self._subscriptions: dict[LifecyclePoint, list[_Subscription]] = {}
        self._sequence = 0
        self._history: deque[LifecycleEvent] = deque(maxlen=history_size)
        self.hook_errors: list[tuple[LifecyclePoint, str]] = []

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------
    def subscribe(
        self,
        point: LifecyclePoint,
        handler: Hook,
        *,
        owner: str = "anonymous",
        priority: int = 100,
        once: bool = False,
    ) -> None:
        """Subscribe a hook. Lower priority values run first."""

        self._sequence += 1
        subscription = _Subscription(
            point=point,
            handler=handler,
            owner=owner,
            priority=priority,
            sequence=self._sequence,
            once=once,
        )
        bucket = self._subscriptions.setdefault(point, [])
        bucket.append(subscription)
        bucket.sort(key=lambda item: (item.priority, item.sequence))

    def unsubscribe(
        self,
        *,
        owner: str | None = None,
        point: LifecyclePoint | None = None,
        handler: Hook | None = None,
    ) -> int:
        """Remove hooks matching the supplied filters. Returns removed count."""

        points: Iterable[LifecyclePoint] = [point] if point else list(self._subscriptions)
        removed = 0
        for current in points:
            bucket = self._subscriptions.get(current, [])
            keep: list[_Subscription] = []
            for subscription in bucket:
                matches = (
                    (owner is None or subscription.owner == owner)
                    and (handler is None or subscription.handler is handler)
                )
                if matches:
                    removed += 1
                else:
                    keep.append(subscription)
            if keep:
                self._subscriptions[current] = keep
            else:
                self._subscriptions.pop(current, None)
        return removed

    def subscribers(self, point: LifecyclePoint) -> tuple[Hook, ...]:
        return tuple(item.handler for item in self._subscriptions.get(point, ()))

    # ------------------------------------------------------------------
    # Snapshot / restore (used by hot-plug transactions)
    # ------------------------------------------------------------------
    def snapshot_subscriptions(self) -> dict[LifecyclePoint, tuple[_Subscription, ...]]:
        """Capture subscriptions so a failed hot-plug operation can roll back."""

        return {
            point: tuple(
                _Subscription(
                    point=item.point,
                    handler=item.handler,
                    owner=item.owner,
                    priority=item.priority,
                    sequence=item.sequence,
                    once=item.once,
                )
                for item in bucket
            )
            for point, bucket in self._subscriptions.items()
        }

    def restore_subscriptions(
        self,
        snapshot: dict[LifecyclePoint, tuple[_Subscription, ...]],
    ) -> None:
        """Replace the subscription table with a previously captured snapshot."""

        self._subscriptions = {
            point: [
                _Subscription(
                    point=item.point,
                    handler=item.handler,
                    owner=item.owner,
                    priority=item.priority,
                    sequence=item.sequence,
                    once=item.once,
                )
                for item in bucket
            ]
            for point, bucket in snapshot.items()
        }

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def can_transition(self, target: LifecycleState) -> bool:
        return target in _TRANSITIONS.get(self.state, frozenset())

    def require_transition(self, target: LifecycleState) -> None:
        if not self.can_transition(target):
            from .errors import InvalidTransitionError

            raise InvalidTransitionError(
                f"illegal lifecycle transition: {self.state.value} -> {target.value}"
            )

    async def transition(
        self,
        target: LifecycleState,
        *,
        point: LifecyclePoint | None = None,
        **payload: Any,
    ) -> LifecycleEvent | None:
        """Validate and apply a transition, then emit its lifecycle point."""

        self.require_transition(target)
        previous = self.state
        self.state = target
        if point is None:
            return None
        return await self.emit(point, previous_state=previous, **payload)

    def set_state_unsafe(self, target: LifecycleState) -> None:
        """Force a state without validation (used only for crash fallback)."""

        self.state = target

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------
    async def emit(self, point: LifecyclePoint, **payload: Any) -> LifecycleEvent:
        """Emit a lifecycle point to all matching hooks.

        Hooks run sequentially in priority order.  A failing hook never breaks
        the harness by default; it is logged and recorded for observability.
        """

        event = LifecycleEvent(
            point=point,
            timestamp=time.time(),
            state=self.state,
            payload=payload,
        )
        self._history.append(event)
        handlers = list(self._subscriptions.get(point, ()))
        for subscription in handlers:
            try:
                result = subscription.handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - hooks are user code
                self.hook_errors.append((point, repr(exc)))
                self.logger.exception("lifecycle hook failed at %s: %s", point.value, exc)
                if self.raise_on_hook_error:
                    raise
            finally:
                if subscription.once:
                    self.unsubscribe(owner=subscription.owner, point=point, handler=subscription.handler)
        return event

    # ------------------------------------------------------------------
    # Observability helpers
    # ------------------------------------------------------------------
    @property
    def history(self) -> tuple[LifecycleEvent, ...]:
        return tuple(self._history)

    def last_event(self, point: LifecyclePoint | None = None) -> LifecycleEvent | None:
        if point is None:
            return self._history[-1] if self._history else None
        for event in reversed(self._history):
            if event.point == point:
                return event
        return None

    async def drain(self) -> None:
        """Yield control to pending tasks (useful in tests/examples)."""

        await asyncio.sleep(0)
