"""Built-in state store.

The state boundary is deliberately an interface: swap this in-memory
implementation for a database / remote service plugin and the rest of the
harness keeps working unchanged (compute-state separation).
"""

from __future__ import annotations

from typing import Any

from ...models import (
    Artifact,
    ContextReference,
    Message,
    Session,
    SessionKind,
    SessionStatus,
    Thread,
    ThreadSubscription,
    Trigger,
)
from ...registry import Capability
from ..base import Plugin, PluginContext, PluginMeta


class InMemoryStore:
    """A complete in-memory implementation of the MemoryStore contract."""

    name = "in_memory"

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.threads: dict[str, Thread] = {}
        self.triggers: dict[str, Trigger] = {}
        self.artifacts: dict[str, Artifact] = {}
        self.subscriptions: dict[tuple[str, str], ThreadSubscription] = {}

    async def save_session(self, session: Session) -> None:
        self.sessions[session.id] = session

    async def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def list_sessions(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[Session]:
        result = list(self.sessions.values())
        if kind is not None:
            result = [item for item in result if item.kind.value == kind or item.kind == kind]
        if status is not None:
            result = [
                item
                for item in result
                if item.status.value == status or item.status == status
            ]
        return result

    async def list_messages(
        self, session_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[Message]:
        session = self.sessions.get(session_id)
        if session is None:
            return []
        start = max(0, offset)
        end = start + max(0, limit)
        return list(session.messages[start:end])

    async def append_message(self, session_id: str, message: Message) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            session.add_message(message)

    async def save_thread(self, thread: Thread) -> None:
        self.threads[thread.id] = thread

    async def get_thread(self, thread_id: str) -> Thread | None:
        return self.threads.get(thread_id)

    async def list_threads(self) -> list[Thread]:
        return list(self.threads.values())

    async def save_trigger(self, trigger: Trigger) -> None:
        self.triggers[trigger.id] = trigger

    async def get_trigger(self, trigger_id: str) -> Trigger | None:
        return self.triggers.get(trigger_id)

    async def save_subscription(self, subscription: ThreadSubscription) -> None:
        key = (subscription.worker_id, subscription.thread_id)
        self.subscriptions[key] = subscription

    async def list_subscriptions(
        self,
        *,
        thread_id: str | None = None,
        worker_id: str | None = None,
    ) -> list[ThreadSubscription]:
        result = list(self.subscriptions.values())
        if thread_id is not None:
            result = [item for item in result if item.thread_id == thread_id]
        if worker_id is not None:
            result = [item for item in result if item.worker_id == worker_id]
        return result

    async def remove_subscription(self, worker_id: str, thread_id: str) -> None:
        self.subscriptions.pop((worker_id, thread_id), None)

    async def save_artifact(self, artifact: Artifact) -> None:
        self.artifacts[artifact.id] = artifact

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        return self.artifacts.get(artifact_id)

    async def resolve_reference(self, reference_id: str) -> ContextReference | None:
        """Resolve an id such as ``session:abc`` or ``thread:xyz``."""

        kind, _, raw_id = reference_id.partition(":")
        if not raw_id:
            return None

        if kind == "trigger":
            trigger = self.triggers.get(raw_id)
            if trigger is None:
                return None
            return ContextReference(
                id=trigger.id,
                kind="trigger",
                summary=f"{trigger.brief(120)} ({trigger.timestamp:.0f})",
                references=list(trigger.references),
            )

        if kind == "thread":
            thread = self.threads.get(raw_id)
            if thread is None:
                return None
            return ContextReference(
                id=thread.id,
                kind="thread",
                summary=(
                    f"thread '{thread.title}' source={thread.source} "
                    f"channel={thread.channel} participants={','.join(thread.participants)}"
                ),
                references=list(thread.references),
            )

        if kind == "session":
            session = self.sessions.get(raw_id)
            if session is None:
                return None
            return ContextReference(
                id=session.id,
                kind="session",
                summary=(
                    f"session {session.kind.value}/{session.status.value} "
                    f"goal={session.goal[:100]}"
                ),
                references=list(session.references),
            )

        if kind == "artifact":
            artifact = self.artifacts.get(raw_id)
            if artifact is None:
                return None
            return ContextReference(
                id=artifact.id,
                kind=artifact.kind,
                summary=artifact.summary,
                references=list(artifact.references),
            )

        return None

    def snapshot(self) -> dict[str, Any]:
        """Cheap introspection for examples, tests and watchdogs."""

        return {
            "sessions": len(self.sessions),
            "threads": len(self.threads),
            "triggers": len(self.triggers),
            "artifacts": len(self.artifacts),
            "subscriptions": len(self.subscriptions),
        }


class InMemoryMemoryPlugin(Plugin):
    meta = PluginMeta(
        name="memory.in_memory",
        version="0.1.0",
        description="In-memory session/thread/trigger/artifact store with chain-of-recall support.",
        provides=("memory",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        context.container.register(
            Capability.MEMORY,
            "in_memory",
            InMemoryStore(),
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


# Kept for API discoverability: these names are frequently used in plugins.
_REFERENCE_TYPES = (Session, SessionKind, SessionStatus, Thread, Trigger, Artifact)
