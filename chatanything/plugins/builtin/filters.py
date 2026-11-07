"""Two-stage trigger filtering: Fast Path rules + Slow Path semantics."""

from __future__ import annotations

from ...models import FilterDecision, FilterStage, Trigger
from ...registry import Capability
from ...runtime import Runtime
from ...utils import extract_json
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import EventFilter

_TASK_KEYWORDS = (
    "帮我",
    "请",
    "修复",
    "实现",
    "添加",
    "支持",
    "检查",
    "确认",
    "排查",
    "更新",
    "创建",
    "处理",
    "回答",
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

_NOISE = ("谢谢", "收到", "哈哈", "好的", "thanks", "thank you", "lol", "+1", "👍", "ok")


def heuristic_semantic(trigger: Trigger, agent_name: str) -> FilterDecision:
    """A deterministic fallback used when the semantic LLM is unavailable."""

    text = trigger.text.strip()
    lowered = text.lower()

    if not text:
        return FilterDecision(False, "empty event", FilterStage.SLOW, 1.0)
    if any(word in lowered for word in _NOISE) and len(text) < 24:
        return FilterDecision(False, "acknowledgement / chatter", FilterStage.SLOW, 0.8)

    agent = agent_name.lower()
    mentioned = agent in lowered or any(name.lower() == agent for name in trigger.mentions)
    if mentioned:
        return FilterDecision(True, "agent explicitly mentioned", FilterStage.SLOW, 0.95)

    if trigger.event_type.startswith(("ci", "review", "pull_request", "issue", "mail", "reminder")):
        return FilterDecision(True, f"actionable event type: {trigger.event_type}", FilterStage.SLOW, 0.8)

    if "?" in text or "？" in text:
        return FilterDecision(True, "contains a question", FilterStage.SLOW, 0.8)

    if any(keyword in lowered for keyword in _TASK_KEYWORDS):
        return FilterDecision(True, "task-like request", FilterStage.SLOW, 0.75)

    if len(text) >= 80:
        return FilterDecision(True, "long contextual message", FilterStage.SLOW, 0.6)

    return FilterDecision(False, "no actionable signal", FilterStage.SLOW, 0.6)


class RuleFilter:
    """Deterministic Fast Path: cheap checks before any model call."""

    name = "rule"
    stage = FilterStage.FAST

    def __init__(
        self,
        *,
        agent_name: str = "ChatAnything",
        blocked_sources: tuple[str, ...] = (),
        ignored_actors: tuple[str, ...] = (),
        ignored_event_types: tuple[str, ...] = ("typing", "presence", "reaction"),
        require_mention: bool = False,
    ) -> None:
        self.agent_name = agent_name
        self.blocked_sources = set(blocked_sources)
        self.ignored_actors = set(ignored_actors)
        self.ignored_event_types = set(ignored_event_types)
        self.require_mention = require_mention
        self.priority = 100

    async def evaluate(self, trigger: Trigger, runtime: Runtime) -> FilterDecision:
        if not trigger.text.strip():
            return FilterDecision(False, "empty text", self.stage)
        if trigger.event_type in self.ignored_event_types:
            return FilterDecision(False, f"ignored event type: {trigger.event_type}", self.stage)
        if trigger.source in self.blocked_sources:
            return FilterDecision(False, f"blocked source: {trigger.source}", self.stage)
        if trigger.actor in self.ignored_actors:
            return FilterDecision(False, f"ignored actor: {trigger.actor}", self.stage)
        if trigger.actor and trigger.actor.lower() == self.agent_name.lower():
            return FilterDecision(False, "self-authored event", self.stage)
        if self.require_mention:
            mentioned = self.agent_name.lower() in trigger.text.lower() or any(
                mention.lower() == self.agent_name.lower() for mention in trigger.mentions
            )
            if not mentioned:
                return FilterDecision(False, "mention required", self.stage)
        return FilterDecision(True, "passed fast path", self.stage, 1.0)


class LLMSemanticFilter:
    """Slow Path: ask the model whether the event deserves attention."""

    name = "semantic"
    stage = FilterStage.SLOW
    priority = 100

    def __init__(self, *, agent_name: str = "ChatAnything", fallback: bool = True) -> None:
        self.agent_name = agent_name
        self.fallback = fallback

    async def evaluate(self, trigger: Trigger, runtime: Runtime) -> FilterDecision:
        try:
            prompt = runtime.resolve(Capability.PROMPT)
            llm = runtime.resolve(Capability.LLM)
            messages = prompt.filter_messages(trigger)  # type: ignore[attr-defined]
            response = await llm.complete(messages)  # type: ignore[attr-defined]
            data = extract_json(response.content)  # type: ignore[attr-defined]
            handle = data.get("handle") if isinstance(data, dict) else None
            if isinstance(handle, str):
                handle = handle.strip().lower() in ("true", "yes", "1", "handle")
            if handle is not None:
                reason = data.get("reason", "semantic decision") if isinstance(data, dict) else ""
                return FilterDecision(bool(handle), str(reason), self.stage, 0.9)
        except Exception as exc:  # noqa: BLE001 - filters must degrade gracefully
            runtime.logger.debug("semantic filter fallback due to %s", exc)

        if self.fallback:
            return heuristic_semantic(trigger, self.agent_name)
        return FilterDecision(True, "semantic filter unavailable; pass by default", self.stage, 0.5)


class RuleFilterPlugin(Plugin):
    meta = PluginMeta(
        name="filter.rule",
        version="0.1.0",
        description="Deterministic Fast Path filter for noise, self-messages and policy rules.",
        provides=("filter.fast",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        agent_name = str(context.setting("agent_name", "ChatAnything"))
        filter_ = RuleFilter(
            agent_name=agent_name,
            blocked_sources=tuple(context.setting("blocked_sources", ())),
            ignored_actors=tuple(context.setting("ignored_actors", ())),
            ignored_event_types=tuple(context.setting("ignored_event_types", ("typing", "presence", "reaction"))),
            require_mention=bool(context.setting("require_mention", False)),
        )
        context.container.register(
            Capability.FILTER, "rule", filter_, plugin=self.meta.name, priority=self.meta.priority
        )


class SemanticFilterPlugin(Plugin):
    meta = PluginMeta(
        name="filter.semantic",
        version="0.1.0",
        description="LLM-based Slow Path semantic filter with deterministic fallback.",
        provides=("filter.slow",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        filter_ = LLMSemanticFilter(
            agent_name=str(context.setting("agent_name", "ChatAnything")),
            fallback=bool(context.setting("fallback", True)),
        )
        context.container.register(
            Capability.FILTER, "semantic", filter_, plugin=self.meta.name, priority=self.meta.priority
        )


_ = EventFilter
