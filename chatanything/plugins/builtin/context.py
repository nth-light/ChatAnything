"""Inline & Spill context organization with chain-of-recall references."""

from __future__ import annotations

import time
from typing import Sequence

from ...lifecycle import LifecyclePoint
from ...models import (
    Artifact,
    ContextBundle,
    ContextReference,
    Message,
    Session,
    Trigger,
)
from ...registry import Capability
from ...runtime import Runtime
from ...utils import estimate_tokens, truncate
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import ContextOrganizer


class InlineSpillContextOrganizer:
    """Treat the context window as a cache.

    Recent and small content is inlined; oversized or older content is spilled
    into artifacts whose ids remain in the window.  A ``recall`` tool can walk
    those ids back through the reference graph (chain of recall).
    """

    name = "inline_spill"

    def __init__(
        self,
        *,
        token_budget: int = 6000,
        inline_token_limit: int = 800,
    ) -> None:
        self.token_budget = token_budget
        self.inline_token_limit = inline_token_limit

    async def build(
        self,
        session: Session,
        runtime: Runtime,
        *,
        pending: Sequence[Trigger] = (),
        budget: int | None = None,
    ) -> ContextBundle:
        total_budget = budget or self.token_budget
        inline_limit = min(self.inline_token_limit, max(64, total_budget // 3))
        await runtime.lifecycle.emit(
            LifecyclePoint.CONTEXT_BUILDING,
            session=session,
            budget=total_budget,
            messages=len(session.messages),
        )

        selected: list[Message] = []
        used = 0
        spilled: list[str] = []
        references: list[ContextReference] = []

        # Walk backwards so the newest content has priority.
        for message in reversed(session.messages):
            tokens = estimate_tokens(message.content)
            if tokens > inline_limit:
                reference = await self._spill(message, session, runtime)
                spilled.append(reference.id)
                references.append(reference)
                content = (
                    truncate(message.content, inline_limit * 2)
                    + f"\n[spilled content: recall('artifact:{reference.id}')]"
                )
                selected.append(Message(role=message.role, content=content, name=message.name))
                used += estimate_tokens(content)
                continue

            if used + tokens > total_budget and len(selected) >= 2:
                # Older content outside the cache window is simply evicted.
                continue

            selected.append(message)
            used += tokens

        selected.reverse()

        for trigger in pending:
            references.append(
                ContextReference(
                    id=trigger.id,
                    kind="trigger",
                    summary=trigger.brief(120),
                    references=list(trigger.references),
                )
            )
        if session.references:
            references.append(
                ContextReference(
                    id=session.id,
                    kind="session",
                    summary=f"{session.kind.value} session goal={session.goal[:80]}",
                    references=list(session.references),
                )
            )

        if spilled:
            hints = ", ".join(f"artifact:{identifier}" for identifier in spilled)
            selected.insert(
                0,
                Message.system(
                    "Context management note: oversized content was spilled. "
                    "Use the recall tool with a reference id to read it. "
                    f"Known references: {hints or 'none'}."
                ),
            )

        bundle = ContextBundle(
            messages=selected,
            references=references,
            spilled=spilled,
            estimated_tokens=used,
            metadata={"budget": total_budget, "inline_limit": inline_limit},
        )
        await runtime.lifecycle.emit(
            LifecyclePoint.CONTEXT_BUILT,
            session=session,
            estimated_tokens=used,
            spilled=spilled,
            references=references,
        )
        return bundle

    async def _spill(
        self, message: Message, session: Session, runtime: Runtime
    ) -> ContextReference:
        artifact = Artifact(
            id=f"spill_{int(time.time() * 1000)}_{len(session.messages)}",
            kind="message",
            summary=f"{message.role} message from session {session.id}",
            content=message.content,
            references=[session.ref, *session.references],
            metadata={"role": message.role, "session_id": session.id, **(message.metadata or {})},
        )
        await runtime.memory.save_artifact(artifact)
        return ContextReference(
            id=artifact.id,
            kind="artifact",
            summary=artifact.summary,
            references=list(artifact.references),
        )


class InlineSpillContextPlugin(Plugin):
    meta = PluginMeta(
        name="context.inline_spill",
        version="0.1.0",
        description="Cache-style context assembly: Inline recent content, Spill the rest.",
        provides=("context",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        organizer = InlineSpillContextOrganizer(
            token_budget=int(context.setting("token_budget", 6000)),
            inline_token_limit=int(context.setting("inline_token_limit", 800)),
        )
        context.container.register(
            Capability.CONTEXT,
            organizer.name,
            organizer,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


_ = ContextOrganizer
