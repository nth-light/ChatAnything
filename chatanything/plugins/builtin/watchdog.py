"""External control loop: detect stalled Workers and repair state."""

from __future__ import annotations

import time

from ...lifecycle import LifecyclePoint
from ...models import WatchdogFinding
from ...registry import Capability
from ...runtime import Runtime
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import Watchdog


class StallWatchdog:
    """Detect sessions that are ``RUNNING`` but have made no progress."""

    name = "stall"

    def __init__(self, *, stall_seconds: float = 120.0) -> None:
        self.stall_seconds = stall_seconds

    async def check(self, runtime: Runtime) -> list[WatchdogFinding]:
        now = time.time()
        findings: list[WatchdogFinding] = []
        sessions = await runtime.memory.list_sessions(kind="worker", status="running")
        for session in sessions:
            idle_for = now - session.last_activity
            if idle_for > self.stall_seconds:
                findings.append(
                    WatchdogFinding(
                        code="worker_stalled",
                        message=f"worker {session.id} idle for {idle_for:.0f}s while running",
                        severity="critical",
                        session_id=session.id,
                        metadata={"idle_for": idle_for, "goal": session.goal},
                    )
                )
        return findings

    async def heal(self, finding: WatchdogFinding, runtime: Runtime) -> bool:
        if finding.session_id is None:
            return False
        session = await runtime.memory.get_session(finding.session_id)
        if session is None or session.status.terminal:
            return False
        from ...models import SessionStatus

        session.status = SessionStatus.FAILED
        session.touch()
        await runtime.memory.save_session(session)
        await runtime.lifecycle.emit(
            LifecyclePoint.SESSION_FAILED,
            session=session,
            reason="watchdog: stalled worker",
            finding=finding,
        )
        runtime.logger.warning("watchdog repaired stalled worker %s", session.id)
        return True


class StallWatchdogPlugin(Plugin):
    meta = PluginMeta(
        name="watchdog.stall",
        version="0.1.0",
        description="Detect and fail stalled Worker sessions.",
        provides=("watchdog",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        watchdog = StallWatchdog(stall_seconds=float(context.setting("stall_seconds", 120.0)))
        context.container.register(
            Capability.WATCHDOG,
            watchdog.name,
            watchdog,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


_ = Watchdog
