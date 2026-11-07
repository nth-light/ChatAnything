"""The default asynchronous ReAct worker loop.

Every session is isolated: it has its own conversation, its own tool calls and
its own token budget.  The loop reports a result; it never talks to humans.
"""

from __future__ import annotations

from typing import Sequence

from ..lifecycle import LifecyclePoint
from ..models import AgentResult, LLMResponse, Message, Session, ToolSpec
from ..registry import Capability
from ..runtime import Runtime
from ..plugins.base import Plugin, PluginContext, PluginMeta
from ..plugins.interfaces import AgentLoop
from .streaming import publish, stream_target
from .tools import ToolExecutor


class ReActAgentLoop:
    """Reason -> act -> observe, repeated until a final answer is produced."""

    name = "react"

    def __init__(self, *, max_steps: int = 6) -> None:
        self.max_steps = max_steps

    async def run(self, session: Session, goal: str, runtime: Runtime) -> AgentResult:
        prompt = runtime.resolve(Capability.PROMPT)
        parser = runtime.resolve(Capability.PARSER)
        llm = runtime.resolve(Capability.LLM)
        organizer = runtime.resolve(Capability.CONTEXT)
        executor = ToolExecutor(runtime)

        tools: Sequence[ToolSpec] = tuple(
            tool.spec for tool in runtime.resolve_all(Capability.TOOL)
        )
        bundle = await organizer.build(session, runtime)  # type: ignore[attr-defined]
        messages: list[Message] = [
            *prompt.worker_messages(session, goal),  # type: ignore[attr-defined]
            *bundle.messages,
        ]

        target = stream_target(runtime, session)
        publish(
            target,
            {
                "type": "start",
                "session_id": session.id,
                "goal": goal,
                "channel": session.metadata.get("channel"),
            },
        )

        usage_total: dict[str, int] = {}
        for step in range(1, self.max_steps + 1):
            session.steps += 1
            session.touch()
            await runtime.lifecycle.emit(
                LifecyclePoint.BEFORE_MODEL_CALL,
                session=session,
                step=step,
                message_count=len(messages),
                tools=[tool.name for tool in tools],
            )
            response = await self._complete(llm, messages, tools, session, target, step)
            for key, value in (response.usage or {}).items():
                usage_total[key] = usage_total.get(key, 0) + int(value)
            await runtime.lifecycle.emit(
                LifecyclePoint.AFTER_MODEL_CALL,
                session=session,
                step=step,
                usage=response.usage,
                tool_calls=response.tool_calls,
            )

            parsed = parser.parse(response.content)  # type: ignore[attr-defined]
            if response.tool_calls:
                parsed.tool_calls = list(response.tool_calls)
            await runtime.lifecycle.emit(
                LifecyclePoint.OUTPUT_PARSED,
                session=session,
                step=step,
                parsed=parsed,
            )

            # Keep clean human-readable finals in session history, while
            # preserving raw assistant payloads for tool-call steps.
            assistant_content = (
                response.content
                if parsed.tool_calls
                else (parsed.final_answer if parsed.final_answer is not None else response.content)
            )
            assistant = Message.assistant(
                assistant_content,
                tool_calls=list(parsed.tool_calls),
                step=step,
            )
            session.add_message(assistant)
            messages.append(assistant)

            if parsed.tool_calls:
                # Drop any text streamed for this step: it was the raw
                # tool-call envelope, not an answer for the user.
                publish(target, {"type": "reset", "step": step})
                for call in parsed.tool_calls:
                    publish(
                        target,
                        {
                            "type": "tool_call",
                            "step": step,
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    )
                results = await executor.execute_all(parsed.tool_calls, session)
                for result in results:
                    publish(
                        target,
                        {
                            "type": "tool_result",
                            "step": step,
                            "call_id": result.call_id,
                            "name": result.name,
                            "is_error": result.is_error,
                            "content": result.content,
                        },
                    )
                    tool_message = Message.tool(
                        result.content,
                        call_id=result.call_id,
                        name=result.name,
                        is_error=result.is_error,
                    )
                    session.add_message(tool_message)
                    messages.append(tool_message)
                continue

            answer = parsed.final_answer if parsed.final_answer is not None else response.content
            answer = (answer or "").strip() or "(empty model response)"
            publish(
                target,
                {
                    "type": "done",
                    "session_id": session.id,
                    "content": answer,
                    "steps": session.steps,
                    "usage": usage_total,
                    "stopped_reason": "completed",
                },
            )
            return AgentResult(
                answer=answer,
                steps=session.steps,
                usage=usage_total,
                stopped_reason="completed",
            )

        limit_answer = (
            f"Worker reached the {self.max_steps}-step limit without a final answer. "
            "Escalate to the Router for a narrower task."
        )
        publish(
            target,
            {
                "type": "done",
                "session_id": session.id,
                "content": limit_answer,
                "steps": session.steps,
                "usage": usage_total,
                "stopped_reason": "max_steps",
            },
        )
        return AgentResult(
            answer=limit_answer,
            steps=session.steps,
            usage=usage_total,
            stopped_reason="max_steps",
        )

    async def _complete(
        self,
        llm: object,
        messages: list[Message],
        tools: Sequence[ToolSpec],
        session: Session,
        target: tuple[object, str] | None,
        step: int,
    ) -> LLMResponse:
        """Call the model, streaming deltas when the provider supports it."""

        astream = getattr(llm, "astream", None)
        if astream is None:
            return await llm.complete(messages, tools=tools, session=session)  # type: ignore[attr-defined]

        streamed = False
        content: list[str] = []
        final: LLMResponse | None = None
        try:
            async for chunk in astream(messages, tools=tools, session=session):
                delta = getattr(chunk, "delta", "") or ""
                if delta:
                    streamed = True
                    content.append(delta)
                    publish(target, {"type": "delta", "step": step, "delta": delta})
                response = getattr(chunk, "response", None)
                if response is not None:
                    final = response
        except Exception:  # noqa: BLE001 - fall back when streaming fails early
            if streamed:
                raise
            return await llm.complete(messages, tools=tools, session=session)  # type: ignore[attr-defined]

        if final is None:
            final = LLMResponse(content="".join(content))
        elif not final.content:
            final.content = "".join(content)
        return final


class ReActAgentLoopPlugin(Plugin):
    meta = PluginMeta(
        name="loop.react",
        version="0.1.0",
        description="Default asynchronous ReAct worker runtime.",
        provides=("agent_loop",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        loop = ReActAgentLoop(max_steps=int(context.setting("max_steps", 6)))
        context.container.register(
            Capability.AGENT_LOOP,
            loop.name,
            loop,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


_ = AgentLoop
