"""Built-in output parsers."""

from __future__ import annotations

from typing import Any

from ...models import ParsedOutput, ToolCall
from ...registry import Capability
from ...utils import extract_json
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import OutputParser


class JsonOutputParser:
    """Parse ReAct-style JSON, OpenAI-style tool_calls, or plain text.

    Supported final shapes::

        {"thought": "...", "action": "calculator", "action_input": {...}}
        {"thought": "...", "final_answer": "..."}
        {"tool_calls": [{"name": "...", "arguments": {...}}]}
        {"answer": "..."}
    """

    name = "json_react"

    def parse(self, text: str) -> ParsedOutput:
        data = extract_json(text)
        if not isinstance(data, dict):
            stripped = text.strip()
            return ParsedOutput(text=stripped, final_answer=stripped if stripped else "")

        thought = data.get("thought") or data.get("reasoning") or ""
        raw_calls = data.get("tool_calls")
        calls: list[ToolCall] = []

        if isinstance(raw_calls, list):
            for item in raw_calls:
                if not isinstance(item, dict):
                    continue
                function = item.get("function") if isinstance(item.get("function"), dict) else item
                name = function.get("name") or item.get("name")
                arguments = function.get("arguments", item.get("arguments", {}))
                if isinstance(arguments, str):
                    parsed = extract_json(arguments)
                    arguments = parsed if isinstance(parsed, dict) else {}
                if name:
                    calls.append(ToolCall(name=str(name), arguments=arguments or {}))

        action = data.get("action")
        if action and action not in ("final", "finish", "final_answer", "none"):
            arguments = data.get("action_input", data.get("arguments", {}))
            if isinstance(arguments, str):
                parsed = extract_json(arguments)
                arguments = parsed if isinstance(parsed, dict) else {}
            calls.append(ToolCall(name=str(action), arguments=arguments or {}))

        if calls:
            return ParsedOutput(text=text, tool_calls=calls)

        final = data.get("final_answer") or data.get("answer") or data.get("final")
        if isinstance(final, dict):
            final = final.get("content") or str(final)
        if final is not None:
            return ParsedOutput(text=text, final_answer=str(final))

        if thought:
            return ParsedOutput(text=text, final_answer=str(thought))
        return ParsedOutput(text=text, final_answer=text.strip())


class JsonOutputParserPlugin(Plugin):
    meta = PluginMeta(
        name="parser.json_react",
        version="0.1.0",
        description="ReAct JSON / OpenAI tool-call output parser.",
        provides=("parser",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        context.container.register(
            Capability.PARSER,
            "json_react",
            JsonOutputParser(),
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


_ = (Any, OutputParser)
