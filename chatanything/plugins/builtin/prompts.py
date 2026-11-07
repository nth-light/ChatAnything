"""Default prompt templates.

Prompts are themselves a plugin: replace this provider to change the persona,
the output format, or to localize the agent without touching the runtime.
"""

from __future__ import annotations

import json
from typing import Sequence

from ...models import Message, Session, Trigger
from ...registry import Capability
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import PromptProvider

FILTER_SYSTEM = """[FILTER]
You are the semantic noise gate of an autonomous agent.
Decide whether the new event genuinely needs agent attention.
Ignore greetings, acknowledgements, emoji-only messages and chatter between other people.
Pay attention when the agent is mentioned, a question is asked, a task is requested,
or a CI/review event must be handled.
Respond with JSON only: {"handle": true|false, "reason": "..."}.
"""

ROUTER_SYSTEM = """[ROUTER]
You are the global Router of a continuous super-agent.
You maintain cross-thread continuity and coordinate isolated Worker sessions.
You never execute tasks yourself. For each trigger choose exactly one action:
- "dispatch": create/reuse a Worker for a concrete task.
- "reply": answer directly when no worker is needed.
- "notify": forward to an existing worker (target_worker).
- "ignore": no action.

Respond with JSON only:
{"decisions": [{"trigger_id": "...", "action": "...", "task": "...", "reply": "...", "thread_id": "...", "target_worker": "...", "reason": "..."}]}
"""

WORKER_SYSTEM = """[WORKER]
You are an isolated Worker session inside a super-agent. Work only on the assigned goal.
Prefer the smallest correct change. Use tools when they provide facts you do not have.
You cannot talk to humans directly; your final answer is reported to the Router.

Respond with JSON only, one step per reply.
To call a tool:
{"thought": "...", "action": "tool_name", "action_input": {...}}
To finish:
{"thought": "...", "final_answer": "..."}
"""


def _json_block(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


class DefaultPromptProvider:
    """The reference prompt set shipped with the harness."""

    name = "default"

    def filter_messages(self, trigger: Trigger) -> list[Message]:
        payload = {
            "event_id": trigger.id,
            "source": trigger.source,
            "channel": trigger.channel,
            "event_type": trigger.event_type,
            "actor": trigger.actor,
            "mentions": list(trigger.mentions),
            "text": trigger.text,
        }
        return [Message.system(FILTER_SYSTEM), Message.user(_json_block(payload))]

    def router_messages(
        self,
        triggers: Sequence[Trigger],
        history: Sequence[Message],
        *,
        agent_name: str,
    ) -> list[Message]:
        payload = {
            "agent": agent_name,
            "triggers": [
                {
                    "trigger_id": trigger.id,
                    "source": trigger.source,
                    "channel": trigger.channel,
                    "thread_id": trigger.thread_id,
                    "actor": trigger.actor,
                    "event_type": trigger.event_type,
                    "mentions": list(trigger.mentions),
                    "text": trigger.text,
                }
                for trigger in triggers
            ],
            "recent_router_history": [message.content for message in history[-6:]],
        }
        return [Message.system(ROUTER_SYSTEM), Message.user(_json_block(payload))]

    def worker_messages(self, session: Session, goal: str) -> list[Message]:
        payload = {
            "session_id": session.id,
            "threads": session.thread_ids,
            "goal": goal,
        }
        return [
            Message.system(WORKER_SYSTEM + "\nCurrent assignment:\n" + _json_block(payload))
        ]


class DefaultPromptPlugin(Plugin):
    meta = PluginMeta(
        name="prompt.default",
        version="0.1.0",
        description="Default filter/router/worker prompt templates.",
        provides=("prompt",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        context.container.register(
            Capability.PROMPT,
            "default",
            DefaultPromptProvider(),
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


_ = PromptProvider  # imported for type-checkers and plugin authors
