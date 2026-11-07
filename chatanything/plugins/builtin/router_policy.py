"""Router decision policies.

The default policy asks the model for a batched JSON plan and falls back to a
deterministic heuristic when no usable model is configured.  Replace it by
registering another ``ROUTER_POLICY`` component.
"""

from __future__ import annotations

from typing import Any, Sequence

from ...models import Message, RouterAction, RouterDecision, RouterPlan, Trigger
from ...registry import Capability
from ...runtime import Runtime
from ...utils import extract_json
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import RouterPolicy

_TASK_SIGNALS = (
    "帮我",
    "请",
    "修复",
    "实现",
    "添加",
    "支持",
    "检查",
    "排查",
    "更新",
    "创建",
    "处理",
    "计算",
    "总结",
    "为什么",
    "怎么",
    "please",
    "help",
    "fix",
    "implement",
    "add",
    "support",
    "check",
    "update",
    "create",
    "review",
    "why",
    "how",
)


def heuristic_decision(trigger: Trigger, agent_name: str) -> RouterDecision:
    """Deterministic fallback used when the Router LLM is unavailable."""

    text = trigger.text.strip()
    lowered = text.lower()
    mentioned = agent_name.lower() in lowered or any(
        mention.lower() == agent_name.lower() for mention in trigger.mentions
    )
    question = "?" in text or "？" in text
    task_like = any(signal in lowered for signal in _TASK_SIGNALS)
    actionable_event = trigger.event_type.startswith(
        ("ci", "review", "pull_request", "issue", "mail", "reminder")
    )

    if mentioned or question or task_like or actionable_event:
        return RouterDecision(
            trigger_id=trigger.id,
            action=RouterAction.DISPATCH,
            task=text,
            thread_id=trigger.thread_id,
            reason="heuristic: actionable signal",
            confidence=0.8,
        )
    return RouterDecision(
        trigger_id=trigger.id,
        action=RouterAction.IGNORE,
        thread_id=trigger.thread_id,
        reason="heuristic: no action needed",
        confidence=0.7,
    )


def heuristic_plan(triggers: Sequence[Trigger], agent_name: str) -> RouterPlan:
    return RouterPlan(decisions=[heuristic_decision(trigger, agent_name) for trigger in triggers])


def _parse_action(value: Any) -> RouterAction | None:
    if isinstance(value, RouterAction):
        return value
    try:
        return RouterAction(str(value).strip().lower())
    except (ValueError, TypeError):
        return None


class LLMRouterPolicy:
    """Batched Router policy with deterministic degradation."""

    name = "llm_router"

    def __init__(self, *, agent_name: str = "ChatAnything") -> None:
        self.agent_name = agent_name

    async def decide(
        self,
        triggers: Sequence[Trigger],
        runtime: Runtime,
        *,
        history: Sequence[Message] = (),
    ) -> RouterPlan:
        try:
            prompt = runtime.resolve(Capability.PROMPT)
            llm = runtime.resolve(Capability.LLM)
            messages = prompt.router_messages(  # type: ignore[attr-defined]
                triggers, history, agent_name=runtime.config.agent_name or self.agent_name
            )
            response = await llm.complete(messages)  # type: ignore[attr-defined]
            data = extract_json(response.content)  # type: ignore[attr-defined]
            raw_decisions = data.get("decisions") if isinstance(data, dict) else None
            if not isinstance(raw_decisions, list):
                raise ValueError("router response has no decisions array")

            by_id: dict[str, dict[str, Any]] = {
                str(item.get("trigger_id")): item
                for item in raw_decisions
                if isinstance(item, dict) and item.get("trigger_id")
            }
            decisions: list[RouterDecision] = []
            for trigger in triggers:
                item = by_id.get(trigger.id)
                if item is None:
                    decisions.append(heuristic_decision(trigger, self.agent_name))
                    continue
                action = _parse_action(item.get("action"))
                if action is None:
                    decisions.append(heuristic_decision(trigger, self.agent_name))
                    continue
                decisions.append(
                    RouterDecision(
                        trigger_id=trigger.id,
                        action=action,
                        task=item.get("task") or (trigger.text if action == RouterAction.DISPATCH else None),
                        reply=item.get("reply"),
                        thread_id=item.get("thread_id") or trigger.thread_id,
                        target_worker=item.get("target_worker"),
                        reason=str(item.get("reason", "")),
                        confidence=float(item.get("confidence", 0.9)),
                        metadata={"source": "llm"},
                    )
                )
            return RouterPlan(decisions=decisions)
        except Exception as exc:  # noqa: BLE001 - degrade instead of crash
            runtime.logger.debug("router LLM fallback: %s", exc)
            return heuristic_plan(triggers, self.agent_name)


class LLMRouterPolicyPlugin(Plugin):
    meta = PluginMeta(
        name="router.llm",
        version="0.1.0",
        description="Batched LLM Router policy with heuristic fallback.",
        provides=("router_policy",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        policy = LLMRouterPolicy(agent_name=str(context.setting("agent_name", "ChatAnything")))
        context.container.register(
            Capability.ROUTER_POLICY,
            policy.name,
            policy,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


_ = RouterPolicy
