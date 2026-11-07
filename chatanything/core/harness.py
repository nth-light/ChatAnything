"""The ChatAnything harness: wiring, lifecycle and event-driven runtime.

Architecture mirrors the Super-agent design document:

    Trigger -> Fast filter -> Router queue -> Router (batched, global)
                                   |                     |
                                   |                Worker(s) (isolated, concurrent)
                                   |                     |
                                   +----- reports <-----+
    Channels <- Router <- reports
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from ..config import HarnessConfig
from ..errors import ChatAnythingError, ComponentNotFoundError, ConfigurationError
from ..lifecycle import LifecycleEngine, LifecyclePoint, LifecycleState
from ..models import (
    FilterDecision,
    FilterStage,
    OutboundMessage,
    Thread,
    Trigger,
    new_id,
)
from ..plugins.builtin import builtin_plugins
from ..plugins.manager import PluginManager
from ..registry import Capability, ComponentRegistry
from ..runtime import ChannelRouter, Runtime
from .filters import FilterPipeline
from .router import RouterAgent
from .worker import WorkerManager


def create_harness(config: HarnessConfig | None = None) -> "Harness":
    """Create an un-started harness (call ``setup`` + ``start``)."""

    return Harness(config)


class Harness:
    """A lifecycle-driven, fully pluggable super-agent harness."""

    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or HarnessConfig()
        self.logger = self._build_logger(self.config)
        self.lifecycle = LifecycleEngine(
            self.logger.getChild("lifecycle"),
            raise_on_hook_error=self.config.raise_on_hook_error,
        )
        self.container = ComponentRegistry()
        self.plugins = PluginManager(
            self.container,
            self.lifecycle,
            logger=self.logger.getChild("plugins"),
            plugin_config=self.config.plugin_config,
            resource_monitoring=self.config.plugin_resource_monitoring,
        )

        self._runtime: Runtime | None = None
        self._filters: FilterPipeline | None = None
        self._router: RouterAgent | None = None
        self._workers: WorkerManager | None = None

        self._router_queue: asyncio.Queue[Trigger] = asyncio.Queue()
        self._pause_gate = asyncio.Event()
        self._pause_gate.set()
        self._stop_event = asyncio.Event()
        self._router_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._sources: list[Any] = []
        self._router_busy = False
        self._running = False
        self._started = False

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    @property
    def runtime(self) -> Runtime:
        if self._runtime is None:
            raise ChatAnythingError("harness is not set up; await harness.setup() first")
        return self._runtime

    @property
    def filters(self) -> FilterPipeline:
        if self._filters is None:
            raise ChatAnythingError("harness is not set up; await harness.setup() first")
        return self._filters

    @property
    def router(self) -> RouterAgent:
        if self._router is None:
            raise ChatAnythingError("harness is not set up; await harness.setup() first")
        return self._router

    @property
    def workers(self) -> WorkerManager:
        if self._workers is None:
            raise ChatAnythingError("harness is not set up; await harness.setup() first")
        return self._workers

    @property
    def state(self) -> LifecycleState:
        return self.lifecycle.state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def setup(self) -> "Harness":
        """Install plugins, resolve defaults and construct runtime services."""

        if self.lifecycle.state != LifecycleState.CREATED:
            raise ChatAnythingError(f"setup() requires CREATED state, got {self.lifecycle.state.value}")
        await self.lifecycle.transition(
            LifecycleState.INITIALIZING,
            point=LifecyclePoint.HARNESS_SETUP,
            config=self.config,
        )
        try:
            if self.config.install_builtin_plugins:
                await self.plugins.install_all(
                    builtin_plugins(),
                    configs=self._builtin_configs(),
                    autostart=False,
                )
            if self.config.plugin_modules:
                await self.plugins.load_references(
                    self.config.plugin_modules,
                    configs=self.config.plugin_config,
                    autostart=False,
                )
            if self.config.discover_entry_points:
                await self.plugins.discover(
                    self.config.entry_point_group,
                    configs=self.config.plugin_config,
                    autostart=False,
                )
            for directory in self.config.plugin_directories:
                await self.plugins.load_directory(directory, autostart=False)

            self._apply_default_overrides()

            memory = self.container.resolve(Capability.MEMORY)
            channels = ChannelRouter(
                self.container,
                default_channel=self.config.default_channel,
                logger=self.logger.getChild("channels"),
            )
            self._runtime = Runtime(
                container=self.container,
                lifecycle=self.lifecycle,
                config=self.config,
                logger=self.logger,
                memory=memory,
                channels=channels,
            )
            self._filters = FilterPipeline(self._runtime)
            self._router = RouterAgent(self)
            await self._router.initialize()
            self._workers = WorkerManager(
                self._runtime,
                max_workers=self.config.max_workers,
                report_callback=self._router.receive_report,
            )

            await self.lifecycle.transition(
                LifecycleState.READY,
                point=LifecyclePoint.HARNESS_READY,
                plugins=self.plugins.installed_names(),
                capabilities=self.container.snapshot(),
            )
            self.logger.info("harness ready with %d plugins", len(self.plugins.installed_names()))
            return self
        except Exception as exc:
            self.logger.exception("harness setup failed")
            await self.lifecycle.emit(
                LifecyclePoint.HARNESS_ERROR, scope="setup", error=repr(exc)
            )
            await self.plugins.shutdown()
            if self.lifecycle.can_transition(LifecycleState.FAILED):
                await self.lifecycle.transition(LifecycleState.FAILED)
            raise

    async def start(self) -> "Harness":
        """Start sources, the Router loop and the external Watchdog loop."""

        if self.lifecycle.state != LifecycleState.READY:
            raise ChatAnythingError(f"start() requires READY state, got {self.lifecycle.state.value}")
        try:
            await self.plugins.start_all()
        except Exception:
            self.logger.exception("plugin startup failed; rolling back started plugins")
            await self.plugins.stop_all()
            raise
        await self.lifecycle.transition(
            LifecycleState.RUNNING,
            point=LifecyclePoint.HARNESS_STARTED,
            agent_name=self.config.agent_name,
        )
        self._running = True
        self._stop_event.clear()

        if self.config.auto_start_sources:
            for source in self.container.resolve_all(Capability.EVENT_SOURCE):
                await source.start(self.submit)  # type: ignore[attr-defined]
                self._sources.append(source)

        self._router_task = asyncio.create_task(self._router_loop(), name="chatanything-router")
        if self.config.watchdog_enabled:
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(), name="chatanything-watchdog"
            )
        self._started = True
        return self

    async def pause(self) -> None:
        if self.lifecycle.state != LifecycleState.RUNNING:
            raise ChatAnythingError("pause() requires RUNNING state")
        self._pause_gate.clear()
        await self.lifecycle.transition(LifecycleState.PAUSED, point=LifecyclePoint.HARNESS_PAUSED)

    async def resume(self) -> None:
        if self.lifecycle.state != LifecycleState.PAUSED:
            raise ChatAnythingError("resume() requires PAUSED state")
        self._pause_gate.set()
        await self.lifecycle.transition(LifecycleState.RUNNING, point=LifecyclePoint.HARNESS_RESUMED)

    async def stop(self) -> None:
        """Stop loops, close sessions and uninstall plugins."""

        state = self.lifecycle.state
        if state in (LifecycleState.CREATED, LifecycleState.STOPPED):
            return
        if self.lifecycle.can_transition(LifecycleState.STOPPING):
            await self.lifecycle.transition(
                LifecycleState.STOPPING, point=LifecyclePoint.HARNESS_STOPPING
            )
        self._running = False
        self._pause_gate.set()
        self._stop_event.set()

        for source in self._sources:
            try:
                await source.stop()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                self.logger.exception("failed to stop event source")
        self._sources.clear()

        for task in (self._router_task, self._watchdog_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    self.logger.exception("background loop stopped with an error")
        self._router_task = None
        self._watchdog_task = None

        if self._workers is not None:
            await self._workers.stop_all()
        if self._router is not None:
            await self._router.close()
        await self.plugins.shutdown()
        self._started = False

        if self.lifecycle.can_transition(LifecycleState.STOPPED):
            await self.lifecycle.transition(
                LifecycleState.STOPPED, point=LifecyclePoint.HARNESS_STOPPED
            )

    async def __aenter__(self) -> "Harness":
        await self.setup()
        try:
            await self.start()
        except Exception:
            await self.stop()
            raise
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------
    async def submit(
        self,
        trigger: Trigger | str,
        *,
        source: str = "console",
        channel: str = "console",
        actor: str | None = "user",
        thread_id: str | None = None,
        mentions: Sequence[str] = (),
        event_type: str = "message",
        metadata: dict[str, Any] | None = None,
    ) -> FilterDecision:
        """Ingest a user message or environmental event.

        Runs the Fast Path filter, then either delivers directly to subscribed
        Workers (thread fast path) or enqueues the trigger for the Router.
        """

        if self.lifecycle.state in (
            LifecycleState.CREATED,
            LifecycleState.STOPPED,
            LifecycleState.FAILED,
        ):
            raise ChatAnythingError(f"cannot submit while harness is {self.lifecycle.state.value}")

        if isinstance(trigger, str):
            trigger = Trigger(
                text=trigger,
                source=source,
                channel=channel,
                actor=actor,
                thread_id=thread_id,
                mentions=tuple(mentions),
                event_type=event_type,
                metadata=metadata or {},
            )

        await self.lifecycle.emit(LifecyclePoint.TRIGGER_INGESTED, trigger=trigger)
        thread = await self.ensure_thread(trigger, trigger.thread_id)
        await self.runtime.memory.save_trigger(trigger)

        verdict = await self.filters.evaluate(trigger, FilterStage.FAST)
        if not verdict.accepted:
            return verdict

        subscriptions = await self.runtime.memory.list_subscriptions(thread_id=thread.id)
        delivered = False
        for subscription in subscriptions:
            worker = self.workers.get(subscription.worker_id)
            if worker is None:
                await self.runtime.memory.remove_subscription(
                    subscription.worker_id, subscription.thread_id
                )
                continue
            if await worker.send(trigger):
                delivered = True

        if delivered:
            await self.lifecycle.emit(
                LifecyclePoint.TRIGGER_ROUTED,
                trigger=trigger,
                fast_path=True,
                reason="thread subscription direct delivery",
            )
            return verdict

        await self._router_queue.put(trigger)
        return verdict

    async def ensure_thread(self, trigger: Trigger, thread_id: str | None = None) -> Thread:
        """Get or create the continuous thread that a trigger belongs to."""

        target_id = thread_id or trigger.thread_id or new_id("thread")
        thread = await self.runtime.memory.get_thread(target_id)
        if thread is None:
            thread = Thread(
                id=target_id,
                source=trigger.source,
                channel=trigger.channel,
                title=" ".join(trigger.text.split())[:80] or "Untitled thread",
                participants=[trigger.actor] if trigger.actor else [],
            )
        else:
            thread.touch()
            if trigger.actor and trigger.actor not in thread.participants:
                thread.participants.append(trigger.actor)
        trigger.thread_id = thread.id
        if thread.ref not in trigger.references:
            trigger.references.append(thread.ref)
        await self.runtime.memory.save_thread(thread)
        return thread

    async def deliver(self, message: OutboundMessage) -> str:
        """Deliver a message through the multi-channel router."""

        return await self.runtime.deliver(message)

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------
    async def _router_loop(self) -> None:
        assert self._router is not None
        try:
            while self._running:
                await self._pause_gate.wait()
                try:
                    first = await self._router_queue.get()
                except asyncio.CancelledError:
                    raise
                self._router_busy = True
                batch = [first]
                deadline = asyncio.get_running_loop().time() + self.config.batch_window_seconds
                while len(batch) < self.config.batch_max_triggers:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        batch.append(
                            await asyncio.wait_for(self._router_queue.get(), timeout=remaining)
                        )
                    except asyncio.TimeoutError:
                        break
                try:
                    await self._router.handle_batch(batch)
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("router batch failed")
                    await self.lifecycle.emit(
                        LifecyclePoint.HARNESS_ERROR,
                        scope="router_loop",
                        error=repr(exc),
                    )
                finally:
                    self._router_busy = False
                await self.lifecycle.emit(
                    LifecyclePoint.HARNESS_TICK,
                    batch_size=len(batch),
                    queue_size=self._router_queue.qsize(),
                )
        except asyncio.CancelledError:
            raise

    async def _watchdog_loop(self) -> None:
        try:
            while self._running:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.config.watchdog_interval,
                    )
                    break  # stop requested
                except asyncio.TimeoutError:
                    pass
                try:
                    watchdog = self.container.resolve(Capability.WATCHDOG)
                except ComponentNotFoundError:
                    continue
                try:
                    findings = await watchdog.check(self.runtime)  # type: ignore[attr-defined]
                    await self.lifecycle.emit(
                        LifecyclePoint.WATCHDOG_CHECKED, findings=findings
                    )
                    for finding in findings:
                        healed = await watchdog.heal(finding, self.runtime)  # type: ignore[attr-defined]
                        if healed:
                            await self.lifecycle.emit(
                                LifecyclePoint.WATCHDOG_RECOVERED, finding=finding
                            )
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("watchdog check failed")
                    await self.lifecycle.emit(
                        LifecyclePoint.HARNESS_ERROR, scope="watchdog", error=repr(exc)
                    )
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Observability / test helpers
    # ------------------------------------------------------------------
    async def wait_until_idle(self, timeout: float = 30.0) -> bool:
        """Wait until the Router queue and all Workers are idle."""

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            busy = (
                self._router_busy
                or not self._router_queue.empty()
                or (self._workers is not None and self._workers.has_active_work())
            )
            if not busy:
                return True
            if self.lifecycle.state not in (LifecycleState.RUNNING, LifecycleState.PAUSED):
                return True
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.02)

    def describe(self) -> dict[str, Any]:
        """A JSON-friendly snapshot for dashboards, logs and examples."""

        store_stats: dict[str, Any] = {}
        if self._runtime is not None and hasattr(self._runtime.memory, "snapshot"):
            store_stats = self._runtime.memory.snapshot()  # type: ignore[attr-defined]
        return {
            "state": self.lifecycle.state.value,
            "plugins": self.plugins.describe(),
            "capabilities": self.container.snapshot(),
            "workers": len(self._workers.workers()) if self._workers else 0,
            "queue": self._router_queue.qsize(),
            "store": store_stats,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _builtin_configs(self) -> dict[str, dict[str, Any]]:
        """Merge global config into per-plugin settings (plugin config wins)."""

        configs = {
            name: dict(values) for name, values in self.config.plugin_config.items()
        }
        defaults: dict[str, dict[str, Any]] = {
            "filter.rule": {"agent_name": self.config.agent_name},
            "filter.semantic": {"agent_name": self.config.agent_name},
            "router.llm": {"agent_name": self.config.agent_name},
            "context.inline_spill": {
                "token_budget": self.config.context_token_budget,
                "inline_token_limit": self.config.inline_token_limit,
            },
            "loop.react": {"max_steps": self.config.max_steps},
            "watchdog.stall": {"stall_seconds": self.config.watchdog_stall_seconds},
        }
        for name, settings in defaults.items():
            configs[name] = {**settings, **configs.get(name, {})}
        return configs

    def _apply_default_overrides(self) -> None:
        for slot_name, component_name in self.config.defaults.items():
            try:
                slot = Capability(slot_name)
            except ValueError as exc:
                raise ConfigurationError(f"unknown capability slot: {slot_name}") from exc
            if not self.container.contains(slot, component_name):
                raise ConfigurationError(
                    f"cannot set default {slot_name}={component_name}: component not registered"
                )
            self.container.set_default(slot, component_name)

    @staticmethod
    def _build_logger(config: HarnessConfig) -> logging.Logger:
        logger = logging.getLogger("chatanything")
        logger.setLevel(config.log_level)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(config.log_format))
            logger.addHandler(handler)
        return logger


__all__ = ["Harness", "create_harness"]
