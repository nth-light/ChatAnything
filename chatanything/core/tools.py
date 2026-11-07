"""Tool execution with lifecycle observability and error normalization."""

from __future__ import annotations

import asyncio
from typing import Sequence

from ..lifecycle import LifecyclePoint
from ..models import Session, ToolCall, ToolResult
from ..registry import Capability
from ..runtime import Runtime
from ..plugins.interfaces import ToolContext, normalize_tool_result


class ToolExecutor:
    """Execute tool calls concurrently inside a Worker session."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def execute_all(
        self,
        calls: Sequence[ToolCall],
        session: Session | None = None,
    ) -> list[ToolResult]:
        if not calls:
            return []
        return list(
            await asyncio.gather(
                *(self.execute(call, session) for call in calls),
                return_exceptions=False,
            )
        )

    async def execute(self, call: ToolCall, session: Session | None = None) -> ToolResult:
        await self.runtime.lifecycle.emit(
            LifecyclePoint.TOOL_CALL_STARTED,
            call=call,
            session=session,
        )
        try:
            tool = self.runtime.resolve(Capability.TOOL, call.name)
        except Exception as exc:
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"Tool {call.name!r} is not registered: {exc}",
                is_error=True,
            )
            await self._finish(result, session)
            return result

        context = ToolContext(runtime=self.runtime, session=session, call=call)
        try:
            raw = await asyncio.wait_for(
                tool.invoke(call.arguments, context),  # type: ignore[attr-defined]
                timeout=self.runtime.config.tool_timeout_seconds,
            )
            result = normalize_tool_result(raw, call)
        except asyncio.TimeoutError:
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"Tool {call.name!r} timed out after "
                f"{self.runtime.config.tool_timeout_seconds}s",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 - tool failures become tool messages
            self.runtime.logger.exception("tool %s failed", call.name)
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"Tool {call.name!r} failed: {exc}",
                is_error=True,
            )
        await self._finish(result, session)
        return result

    async def _finish(self, result: ToolResult, session: Session | None) -> None:
        await self.runtime.lifecycle.emit(
            LifecyclePoint.TOOL_CALL_FINISHED,
            result=result,
            session=session,
        )
