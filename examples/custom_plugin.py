"""Custom plugin example: replace the LLM and observe the lifecycle.

Run::

    python examples/custom_plugin.py

This demonstrates three extension mechanisms without touching framework code:

1. a new ``LLMProvider`` plugin replaces the default model;
2. a lifecycle hook plugin audits tool calls and reports;
3. the new LLM drives the built-in ``calculator`` tool through ReAct.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatanything import (
    Capability,
    Harness,
    HarnessConfig,
    LLMResponse,
    LifecyclePoint,
    Message,
    Plugin,
    PluginContext,
    PluginMeta,
    ToolSpec,
)
from chatanything.utils import extract_json


class ScriptedLLM:
    """A deterministic 'model' that follows the framework's prompt contracts."""

    name = "scripted"

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        session=None,
    ) -> LLMResponse:
        system = next((message.content for message in messages if message.role == "system"), "")
        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )

        if "[FILTER]" in system:
            payload = extract_json(last_user) or {}
            text = str(payload.get("text", ""))
            handle = any(token in text for token in ("计算", "请", "?", "？"))
            return LLMResponse(
                content=json.dumps({"handle": handle, "reason": "scripted semantic filter"})
            )

        if "[ROUTER]" in system:
            payload = extract_json(last_user) or {}
            decisions = []
            for trigger in payload.get("triggers", []):
                decisions.append(
                    {
                        "trigger_id": trigger.get("trigger_id"),
                        "action": "dispatch",
                        "task": trigger.get("text"),
                        "reason": "scripted router: actionable request",
                    }
                )
            return LLMResponse(content=json.dumps({"decisions": decisions}, ensure_ascii=False))

        if "[WORKER]" in system:
            tool_messages = [message for message in messages if message.role == "tool"]
            if tool_messages:
                answer = tool_messages[-1].content
                return LLMResponse(
                    content=json.dumps(
                        {"thought": "tool result received", "final_answer": f"计算结果是 {answer}."},
                        ensure_ascii=False,
                    )
                )
            match = re.search(r"([0-9][0-9\s+\-*/%().]*)", last_user)
            if match:
                expression = match.group(1).strip()
                return LLMResponse(
                    content=json.dumps(
                        {
                            "thought": "need arithmetic",
                            "action": "calculator",
                            "action_input": {"expression": expression},
                        },
                        ensure_ascii=False,
                    )
                )
            return LLMResponse(
                content=json.dumps({"thought": "done", "final_answer": "任务已完成。"}, ensure_ascii=False)
            )

        return LLMResponse(content="ScriptedLLM: no matching prompt contract.")


class ScriptedLLMPlugin(Plugin):
    meta = PluginMeta(
        name="example.scripted_llm",
        version="0.1.0",
        description="Deterministic model used to demonstrate plugin replacement.",
        provides=("llm",),
        priority=10,
    )

    async def install(self, context: PluginContext) -> None:
        context.container.register(
            Capability.LLM,
            "scripted",
            ScriptedLLM(),
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


class AuditPlugin(Plugin):
    """Observe tool calls and final reports through lifecycle hooks."""

    meta = PluginMeta(
        name="example.audit",
        version="0.1.0",
        description="Audit tool calls and worker reports.",
        priority=20,
    )

    def hooks(self):
        return {
            LifecyclePoint.TOOL_CALL_FINISHED: self._on_tool,
            LifecyclePoint.SESSION_REPORTED: self._on_report,
        }

    async def install(self, context: PluginContext) -> None:
        self.audit_log: list[str] = []

    async def _on_tool(self, event) -> None:
        result = event.payload["result"]
        entry = f"tool {result.name} -> {result.content}"
        self.audit_log.append(entry)
        print(f"  [audit] {entry}")

    async def _on_report(self, event) -> None:
        report = event.payload["report"]
        print(f"  [audit] worker {report.session_id} finished in {report.steps} step(s)")


async def main() -> None:
    harness = Harness(HarnessConfig(log_level=logging.WARNING))
    await harness.setup()

    # Runtime replacement: install after setup and switch the slot default.
    await harness.plugins.install(ScriptedLLMPlugin())
    harness.container.set_default(Capability.LLM, "scripted")
    await harness.plugins.install(AuditPlugin())

    await harness.start()
    try:
        await harness.submit(
            "请计算 12 * (3 + 4) 的结果，并告诉我最终数值。",
            actor="bob",
            thread_id="thread-calc",
        )
        idle = await harness.wait_until_idle(timeout=15)
        print("\nidle:", idle)
        print(json.dumps(harness.describe(), ensure_ascii=False, indent=2, default=str))
    finally:
        await harness.stop()


if __name__ == "__main__":
    asyncio.run(main())
