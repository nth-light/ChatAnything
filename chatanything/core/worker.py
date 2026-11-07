"""Worker layer: isolated, concurrent execution sessions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable

from ..lifecycle import LifecyclePoint
from ..models import (
    Message,
    Session,
    SessionKind,
    SessionStatus,
    Thread,
    ThreadSubscription,
    Trigger,
    WorkerReport,
    new_id,
)
from ..runtime import Runtime
from ..registry import Capability
from .streaming import publish_trigger

if TYPE_CHECKING:
    from .harness import Harness


class WorkerSession:
    """One isolated execution context subscribed to one or more threads."""

    def __init__(
        self,
        runtime: Runtime,
        session: Session,
        thread: Thread,
        initial_trigger: Trigger,
        goal: str,
        report_callback: Callable[[WorkerReport], Awaitable[None]] | None,
    ) -> None:
        self.runtime = runtime
        self.session = session
        self.thread = thread
        self.initial_trigger = initial_trigger
        self.goal = goal
        self.report_callback = report_callback
        self.inbox: asyncio.Queue[Trigger] = asyncio.Queue()
        self._closed = False
        self._trigger_id: str = initial_trigger.id

        self.session.messages.append(
            Message.user(
                initial_trigger.text,
                trigger_id=initial_trigger.id,
                source=initial_trigger.source,
                channel=initial_trigger.channel,
                references=[initial_trigger.ref, thread.ref],
            )
        )

    @property
    def id(self) -> str:
        return self.session.id

    @property
    def status(self) -> SessionStatus:
        return self.session.status

    @property
    def has_pending(self) -> bool:
        return not self.inbox.empty()

    async def send(self, trigger: Trigger) -> bool:
        if self._closed or self.session.status.terminal:
            return False
        self.inbox.put_nowait(trigger)
        return True

    async def run(self) -> None:
        try:
            first = True
            while not self._closed:
                if first:
                    first = False
                else:
                    self.session.status = SessionStatus.WAITING
                    self.session.touch()
                    await self.runtime.memory.save_session(self.session)
                    try:
                        trigger = await asyncio.wait_for(
                            self.inbox.get(),
                            timeout=self.runtime.config.worker_idle_timeout,
                        )
                    except asyncio.TimeoutError:
                        await self.close("idle timeout")
                        break
                    self._trigger_id = trigger.id
                    self.session.status = SessionStatus.RUNNING
                    self.session.touch()
                    self.session.messages.append(
                        Message.user(
                            trigger.text,
                            trigger_id=trigger.id,
                            source=trigger.source,
                            channel=trigger.channel,
                            references=[trigger.ref, self.thread.ref],
                        )
                    )
                self.session.status = SessionStatus.RUNNING
                self.session.touch()
                await self.runtime.memory.save_session(self.session)
                if self.session.steps == 0:
                    await self.runtime.lifecycle.emit(
                        LifecyclePoint.SESSION_STARTED,
                        session=self.session,
                        thread=self.thread,
                    )
                finished = await self._execute()
                if not finished:
                    break
        except asyncio.CancelledError:
            await self.close("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - harness resilience
            self.runtime.logger.exception("worker %s crashed", self.session.id)
            await self._fail(str(exc))
        finally:
            if not self._closed:
                await self.close("finished")

    async def _execute(self) -> bool:
        loop = self.runtime.resolve(Capability.AGENT_LOOP)
        try:
            result = await loop.run(self.session, self.goal, self.runtime)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            await self._fail(str(exc))
            return False

        await self.runtime.memory.save_session(self.session)
        report = WorkerReport(
            session_id=self.session.id,
            thread_id=self.thread.id,
            channel=self.session.metadata.get("channel", self.runtime.config.default_channel),
            answer=result.answer,
            steps=result.steps,
            error=result.error,
            usage=result.usage,
            metadata={"stopped_reason": result.stopped_reason},
        )
        await self.runtime.lifecycle.emit(
            LifecyclePoint.SESSION_REPORTED,
            session=self.session,
            report=report,
        )
        if self.report_callback is not None:
            await self.report_callback(report)
        self.session.status = SessionStatus.WAITING
        self.session.touch()
        await self.runtime.memory.save_session(self.session)
        return True

    async def _fail(self, error: str) -> None:
        self.session.status = SessionStatus.FAILED
        self.session.touch()
        await self.runtime.memory.save_session(self.session)
        publish_trigger(
            self.runtime,
            self._trigger_id,
            {
                "type": "error",
                "message": error,
                "content": f"Worker failed: {error}",
                "stopped_reason": "failed",
            },
        )
        await self.runtime.lifecycle.emit(
            LifecyclePoint.SESSION_FAILED,
            session=self.session,
            error=error,
        )
        if self.report_callback is not None:
            await self.report_callback(
                WorkerReport(
                    session_id=self.session.id,
                    thread_id=self.thread.id,
                    channel=self.session.metadata.get("channel", self.runtime.config.default_channel),
                    answer=f"Worker failed: {error}",
                    steps=self.session.steps,
                    error=error,
                )
            )
        await self.close("failed")

    async def close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        for thread_id in self.session.thread_ids:
            await self.runtime.memory.remove_subscription(self.session.id, thread_id)
        if not self.session.status.terminal:
            self.session.status = SessionStatus.CLOSED
        self.session.touch()
        await self.runtime.memory.save_session(self.session)
        await self.runtime.lifecycle.emit(
            LifecyclePoint.SESSION_CLOSED,
            session=self.session,
            reason=reason,
        )


class WorkerManager:
    """Spawn, track and stop Worker sessions with bounded concurrency."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        max_workers: int = 4,
        report_callback: Callable[[WorkerReport], Awaitable[None]] | None = None,
    ) -> None:
        self.runtime = runtime
        self.max_workers = max_workers
        self.report_callback = report_callback
        self._workers: dict[str, WorkerSession] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(max_workers)

    async def spawn(self, thread: Thread, trigger: Trigger, task: str) -> WorkerSession:
        session = Session(
            id=new_id("worker"),
            kind=SessionKind.WORKER,
            title=f"Worker: {task[:60]}",
            goal=task,
            status=SessionStatus.CREATED,
            thread_ids=[thread.id],
            parent_id="session_router",
            references=[thread.ref, trigger.ref],
            metadata={
                "channel": trigger.channel,
                "source": trigger.source,
                "trigger_id": trigger.id,
            },
        )
        await self.runtime.memory.save_session(session)
        await self.runtime.memory.save_subscription(
            ThreadSubscription(
                worker_id=session.id,
                thread_id=thread.id,
                session_id=session.id,
            )
        )
        await self.runtime.lifecycle.emit(
            LifecyclePoint.SESSION_CREATED,
            session=session,
            kind=session.kind.value,
            thread=thread,
        )

        worker = WorkerSession(
            runtime=self.runtime,
            session=session,
            thread=thread,
            initial_trigger=trigger,
            goal=task,
            report_callback=self.report_callback,
        )
        self._workers[session.id] = worker
        task_handle = asyncio.create_task(self._run(worker))
        self._tasks.add(task_handle)
        task_handle.add_done_callback(lambda _: self._cleanup(session.id, task_handle))
        return worker

    async def _run(self, worker: WorkerSession) -> None:
        async with self._semaphore:
            await worker.run()

    def _cleanup(self, worker_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.runtime.logger.error("worker task failed: %s", task.exception())
        worker = self._workers.get(worker_id)
        if worker is not None and worker.session.status.terminal:
            self._workers.pop(worker_id, None)

    def get(self, worker_id: str) -> WorkerSession | None:
        worker = self._workers.get(worker_id)
        if worker is None:
            return None
        if worker.session.status.terminal:
            self._workers.pop(worker_id, None)
            return None
        return worker

    def workers(self) -> tuple[WorkerSession, ...]:
        return tuple(self._workers.values())

    def has_active_work(self) -> bool:
        return any(
            worker.session.status.active or worker.has_pending
            for worker in self._workers.values()
        )

    async def stop_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for worker in list(self._workers.values()):
            if not worker.session.status.terminal:
                await worker.close("shutdown")
        self._workers.clear()
