"""Built-in tools and the ``FunctionTool`` adapter.

Tools are plugins too: register a :class:`ToolSpec` plus an async callable and
the ReAct loop can use it immediately.
"""

from __future__ import annotations

import ast
import inspect
import operator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from ...models import ToolCall, ToolResult, ToolSpec
from ...registry import Capability
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import Tool, ToolContext, normalize_tool_result

ToolHandler = Callable[[Mapping[str, Any], ToolContext], Awaitable[str | ToolResult] | str | ToolResult]

_BINARY_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        return _BINARY_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("only arithmetic expressions are supported")


@dataclass(slots=True)
class FunctionTool:
    """Wrap a plain Python callable as a model-visible tool."""

    spec: ToolSpec
    handler: ToolHandler

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> str | ToolResult:
        result = self.handler(arguments, context)
        if inspect.isawaitable(result):
            result = await result
        call = context.call or ToolCall(name=self.spec.name, arguments=dict(arguments))
        return normalize_tool_result(result, call)


class CalculatorTool:
    """A safe arithmetic evaluator (no ``eval``, only AST whitelist)."""

    spec = ToolSpec(
        name="calculator",
        description="Evaluate a basic arithmetic expression (+, -, *, /, %, **).",
        parameters={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Arithmetic expression, e.g. 12 * (3 + 4)"}
            },
            "required": ["expression"],
        },
    )

    async def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> str:
        expression = str(arguments.get("expression", "")).strip()
        if not expression:
            raise ValueError("expression is required")
        tree = ast.parse(expression, mode="eval")
        return str(_eval_node(tree))


class CurrentTimeTool:
    """Return the current UTC or local time."""

    spec = ToolSpec(
        name="current_time",
        description="Get the current time, optionally in an IANA timezone.",
        parameters={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone like Asia/Shanghai; omit for UTC.",
                }
            },
        },
    )

    async def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> str:
        zone_name = str(arguments.get("timezone", "")).strip()
        if zone_name:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo(zone_name))
        else:
            now = datetime.now(timezone.utc)
        return now.isoformat()


class RecallTool:
    """Walk the reference graph to recall spilled / historical context."""

    spec = ToolSpec(
        name="recall",
        description=(
            "Recall a trigger/thread/session/artifact by reference id. "
            "Follows linked references up to the requested depth."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reference_id": {
                    "type": "string",
                    "description": "e.g. artifact:spill_123, thread:thread_abc, session:worker_xyz",
                },
                "depth": {"type": "integer", "description": "How many reference hops to follow (default 2)"},
            },
            "required": ["reference_id"],
        },
    )

    async def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> str:
        reference_id = str(arguments.get("reference_id", "")).strip()
        depth = int(arguments.get("depth", 2))
        if not reference_id:
            raise ValueError("reference_id is required")

        prefixes = ("artifact", "trigger", "thread", "session")
        candidates = (
            [reference_id]
            if ":" in reference_id
            else [f"{prefix}:{reference_id}" for prefix in prefixes]
        )

        memory = context.runtime.memory
        visited: set[str] = set()
        lines: list[str] = []

        async def walk(ref: str, remaining: int, indent: str) -> None:
            if remaining < 0 or ref in visited:
                return
            visited.add(ref)
            direct = await memory.resolve_reference(ref)
            if direct is None:
                return
            lines.append(f"{indent}- {ref} [{direct.kind}] {direct.summary}")
            if direct.kind == "artifact":
                artifact = await memory.get_artifact(direct.id)
                if artifact is not None:
                    lines.append(f"{indent}  content: {artifact.content}")
            for child in direct.references:
                await walk(child, remaining - 1, indent + "  ")

        for candidate in candidates:
            found = await memory.resolve_reference(candidate)
            if found is not None:
                await walk(candidate, depth, "")
                break
        else:
            return f"No reference found for {reference_id!r}."
        return "\n".join(lines)


class CoreToolsPlugin(Plugin):
    meta = PluginMeta(
        name="tools.core",
        version="0.1.0",
        description="Calculator, current_time and chain-of-recall tools.",
        provides=("tool",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        for tool in (CalculatorTool(), CurrentTimeTool(), RecallTool()):
            context.container.register(
                Capability.TOOL,
                tool.spec.name,
                tool,
                plugin=self.meta.name,
                priority=self.meta.priority,
                default=False,
            )


_ = (Tool, ToolHandler)
