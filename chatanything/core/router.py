"""Router layer: the global sequence point.

The Router owns cross-thread continuity and decides what to do with batched
triggers.  It never executes concrete tasks itself and never uses the Worker
toolset.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from ..lifecycle import LifecyclePoint
from ..models import (
    FilterStage,
    Message,
    OutboundMessage,
    RouterAction,
    RouterDecision,
    RouterPlan,
    Session,
    SessionKind,
    SessionStatus,
    Thread,
    Trigger,
    WorkerReport,
)
from ..registry import Capability
from ..runtime import Runtime
from .streaming import publish_trigger

if TYPE_CHECKING:
    from .harness import Harness


class RouterAgent:
    """The long-running Router session."""

    def __init__(self, harness: "Harness") -> None:
        self.harness = harness
        self.runtime: Runtime = harness.runtime
        self.memory = harness.runtime.memory
        self.lifecycle = harness.runtime.lifecycle
        self.session: Session | None = None

    async def initialize(self) -> None:
        self.session = Session(
            id="session_router",
            kind=SessionKind.ROUTER,
            title="Global Router",
            goal="Maintain continuity and coordinate Worker sessions.",
            status=SessionStatus.RUNNING,
        )
        await self.memory.save_session(self.session)
        await self.lifecycle.emit(
            LifecyclePoint.SESSION_CREATED,
            session=self.session,
            kind=self.session.kind.value,
        )

    # ------------------------------------------------------------------
    # Batched event handling
    # ------------------------------------------------------------------
    async def handle_batch(self, triggers: Sequence[Trigger]) -> None:
        assert self.session is not None
        accepted: list[Trigger] = []
        for trigger in triggers:
            verdict = await self.harness.filters.evaluate(trigger, stage=FilterStage.SLOW)
            if verdict.accepted:
                accepted.append(trigger)
        if not accepted:
            return

        history = tuple(self.session.messages[-10:])
        policy = self.runtime.resolve(Capability.ROUTER_POLICY)
        plan: RouterPlan = await policy.decide(accepted, self.runtime, history=history)  # type: ignore[attr-defined]

        speculations: dict[str, object] = {}
        if self.runtime.config.speculation_enabled:
            try:
                speculator = self.runtime.resolve(Capability.SPECULATOR)
                for trigger in accepted:
                    speculation = await speculator.prepare(trigger, self.runtime)  # type: ignore[attr-defined]
                    if speculation is not None:
                        speculations[trigger.id] = speculation
                        await self.lifecycle.emit(
                            LifecyclePoint.SPECULATION_PREPARED,
                            trigger=trigger,
                            speculation=speculation,
                        )
            except Exception as exc:  # noqa: BLE001 - speculation is best effort
                self.runtime.logger.debug("speculation skipped: %s", exc)

        for trigger in accepted:
            decision = plan.for_trigger(trigger.id)
            if decision is None:
                decision = RouterDecision(
                    trigger_id=trigger.id,
                    action=RouterAction.IGNORE,
                    reason="router produced no decision",
                )
            try:
                await self._apply(trigger, decision, speculations.get(trigger.id))
            except Exception as exc:  # noqa: BLE001 - one bad decision must not stop the batch
                self.runtime.logger.exception("router decision failed: %s", exc)
                publish_trigger(
                    self.runtime,
                    trigger.id,
                    {
                        "type": "error",
                        "message": str(exc),
                        "content": f"处理失败：{exc}",
                        "stopped_reason": "router_error",
                    },
                )
                await self.lifecycle.emit(
                    LifecyclePoint.HARNESS_ERROR,
                    scope="router",
                    trigger=trigger,
                    error=repr(exc),
                )

        await self._record(accepted, plan)

    async def _apply(
        self,
        trigger: Trigger,
        decision: RouterDecision,
        speculation: object | None,
    ) -> None:
        await self.lifecycle.emit(
            LifecyclePoint.TRIGGER_ROUTED,
            trigger=trigger,
            decision=decision,
        )

        if decision.action == RouterAction.IGNORE:
            reason = decision.reason or "router ignored"
            await self.lifecycle.emit(
                LifecyclePoint.TRIGGER_IGNORED,
                trigger=trigger,
                reason=reason,
            )
            publish_trigger(
                self.runtime,
                trigger.id,
                {
                    "type": "done",
                    "content": "",
                    "reason": reason,
                    "stopped_reason": "ignored",
                },
            )
            return

        if decision.action == RouterAction.REPLY:
            reply = decision.reply or ""
            publish_trigger(
                self.runtime,
                trigger.id,
                {"type": "start", "session_id": "router", "goal": trigger.text},
            )
            await self.runtime.deliver(
                OutboundMessage(
                    channel=trigger.channel,
                    text=reply,
                    thread_id=decision.thread_id or trigger.thread_id,
                    kind="reply",
                    metadata={"trigger_id": trigger.id, "reason": decision.reason},
                )
            )
            publish_trigger(
                self.runtime,
                trigger.id,
                {
                    "type": "done",
                    "content": reply,
                    "reason": decision.reason,
                    "stopped_reason": "replied",
                },
            )
            return

        if decision.action == RouterAction.NOTIFY:
            worker = self.harness.workers.get(decision.target_worker or "")
            if worker is not None and await worker.send(trigger):
                return
            # Fall through to a fresh dispatch if the target is gone.

        thread: Thread = await self.harness.ensure_thread(trigger, decision.thread_id)
        task = decision.task or trigger.text
        if speculation is not None:
            try:
                speculator = self.runtime.resolve(Capability.SPECULATOR)
                adopted = await speculator.evaluate(trigger, speculation, self.runtime)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                adopted = False
            if adopted:
                task += f"\n\nSpeculative findings:\n{getattr(speculation, 'result', '')}"
                await self.lifecycle.emit(
                    LifecyclePoint.SPECULATION_ADOPTED,
                    trigger=trigger,
                    speculation=speculation,
                )
            else:
                await self.lifecycle.emit(
                    LifecyclePoint.SPECULATION_DISCARDED,
                    trigger=trigger,
                    speculation=speculation,
                )

        await self.harness.workers.spawn(thread=thread, trigger=trigger, task=task)

    async def _record(self, triggers: Sequence[Trigger], plan: RouterPlan) -> None:
        assert self.session is not None
        summary = "\n".join(f"- {trigger.brief()}" for trigger in triggers)
        self.session.add_message(Message.user(f"Triggers:\n{summary}"))
        decisions = "\n".join(
            f"- {decision.trigger_id}: {decision.action.value} ({decision.reason})"
            for decision in plan.decisions
        )
        self.session.add_message(Message.assistant(f"Router decisions:\n{decisions}"))
        await self.memory.save_session(self.session)

    # ------------------------------------------------------------------
    # Worker reports
    # ------------------------------------------------------------------
    async def receive_report(self, report: WorkerReport) -> None:
        assert self.session is not None
        await self.lifecycle.emit(LifecyclePoint.REPORT_RECEIVED, report=report)
        await self.runtime.deliver(
            OutboundMessage(
                channel=report.channel or self.runtime.config.default_channel,
                text=report.answer,
                thread_id=report.thread_id,
                session_id=report.session_id,
                kind="report",
                metadata={"steps": report.steps, "error": report.error},
            )
        )
        self.session.add_message(
            Message.assistant(
                f"[worker {report.session_id} -> thread {report.thread_id}] {report.answer}"
            )
        )
        await self.memory.save_session(self.session)

    async def close(self) -> None:
        if self.session is not None and not self.session.status.terminal:
            self.session.status = SessionStatus.CLOSED
            self.session.touch()
            await self.memory.save_session(self.session)
            await self.lifecycle.emit(LifecyclePoint.SESSION_CLOSED, session=self.session)
