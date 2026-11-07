"""Built-in model providers.

``echo`` is deterministic and dependency-free, so examples and tests run
without credentials.  ``openai_compatible`` talks to any OpenAI-style
``/chat/completions`` endpoint using only the standard library.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any, AsyncIterator, Sequence

from ...models import LLMResponse, LLMStreamChunk, Message, Session, ToolCall, ToolSpec
from ...registry import Capability
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import LLMProvider


def _message_to_openai(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
        if not message.content:
            payload.pop("content")
    return payload


def _tool_to_openai(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _parse_tool_calls(raw_calls: Sequence[Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{index}"),
                name=str(function.get("name", "")),
                arguments=_parse_arguments(function.get("arguments")),
            )
        )
    return calls


def _merge_tool_call_deltas(
    calls: dict[int, dict[str, Any]],
    deltas: Sequence[Any],
) -> None:
    """Accumulate OpenAI streaming tool-call fragments keyed by ``index``."""

    for offset, item in enumerate(deltas):
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            index = offset
        slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if item.get("id"):
            slot["id"] = str(item["id"])
        function = item.get("function") or {}
        name = function.get("name")
        if isinstance(name, str) and name:
            slot["name"] = name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["arguments"] += arguments


def _build_tool_calls(calls: dict[int, dict[str, Any]]) -> list[ToolCall]:
    return [
        ToolCall(
            id=slot["id"] or f"call_{index}",
            name=slot["name"],
            arguments=_parse_arguments(slot["arguments"]),
        )
        for index, slot in sorted(calls.items())
        if slot.get("name")
    ]


def _deliver(loop: Any, events: "asyncio.Queue[Any]", item: Any) -> None:
    """Thread-safe hand-off from the HTTP pump thread to the event loop."""

    try:
        loop.call_soon_threadsafe(events.put_nowait, item)
    except RuntimeError:  # event loop already closed
        pass


class EchoLLM:
    """A deterministic offline model used by the default quickstart."""

    name = "echo"

    def __init__(self, *, chunk_size: int = 2, stream_delay: float = 0.008) -> None:
        self.chunk_size = max(1, int(chunk_size))
        self.stream_delay = max(0.0, float(stream_delay))

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        session: Session | None = None,
    ) -> LLMResponse:
        last_user = ""
        for message in reversed(messages):
            if message.role == "user":
                last_user = message.content
                break
        content = f"Echo: {last_user}" if last_user else "Echo: (no user message)"
        approx_tokens = max(1, len(content) // 4)
        return LLMResponse(
            content=content,
            usage={"prompt_tokens": 0, "completion_tokens": approx_tokens},
        )

    async def astream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        session: Session | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Emit the echo answer piece by piece to mimic a streaming model."""

        response = await self.complete(messages, tools=tools, session=session)
        content = response.content
        for index in range(0, len(content), self.chunk_size):
            if self.stream_delay:
                await asyncio.sleep(self.stream_delay)
            yield LLMStreamChunk(delta=content[index : index + self.chunk_size])
        yield LLMStreamChunk(response=response, usage=response.usage)


class OpenAICompatibleLLM:
    """Minimal OpenAI-compatible chat completion client (stdlib only)."""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout: float = 60.0,
        temperature: float = 0.2,
        endpoint: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.timeout = timeout
        self.temperature = temperature
        self.endpoint = endpoint or f"{self.base_url}/chat/completions"
        self.extra_headers = extra_headers or {}

    def _payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_openai(message) for message in messages],
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = [_tool_to_openai(tool) for tool in tools]
        return payload

    def _open(self, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        request = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._open(payload) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        session: Session | None = None,
    ) -> LLMResponse:
        payload = self._payload(messages, tools)
        data = await asyncio.to_thread(self._post, payload)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = _parse_tool_calls(message.get("tool_calls") or [])
        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=calls,
            usage={key: int(value) for key, value in usage.items() if isinstance(value, int)},
            raw=data,
        )

    async def astream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        session: Session | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Stream ``/chat/completions`` chunks parsed from server-sent events."""

        payload = self._payload(messages, tools)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

        loop = asyncio.get_running_loop()
        events: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()

        def pump() -> None:
            try:
                with self._open(payload) as response:
                    for raw_line in response:
                        line = raw_line.decode("utf-8", errors="ignore").strip()
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            parsed = json.loads(data)
                        except ValueError:
                            continue
                        _deliver(loop, events, parsed)
            except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
                _deliver(loop, events, exc)
            finally:
                _deliver(loop, events, sentinel)

        asyncio.create_task(asyncio.to_thread(pump))
        text: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] = {}
        while True:
            item = await events.get()
            if item is sentinel:
                break
            if isinstance(item, BaseException):
                raise item
            if not isinstance(item, dict):
                continue
            for key, value in (item.get("usage") or {}).items():
                if isinstance(value, int):
                    usage[key] = int(value)
            for choice in item.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content") or ""
                if content:
                    text.append(content)
                    yield LLMStreamChunk(delta=content)
                _merge_tool_call_deltas(calls, delta.get("tool_calls") or [])

        yield LLMStreamChunk(
            response=LLMResponse(
                content="".join(text),
                tool_calls=_build_tool_calls(calls),
                usage=usage,
            ),
            usage=usage,
        )


class EchoLLMPlugin(Plugin):
    meta = PluginMeta(
        name="llm.echo",
        version="0.1.0",
        description="Deterministic offline LLM used for demos and tests.",
        provides=("llm",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        model = EchoLLM(
            chunk_size=int(context.setting("chunk_size", 2)),
            stream_delay=float(context.setting("stream_delay", 0.008)),
        )
        context.container.register(
            Capability.LLM, "echo", model, plugin=self.meta.name, priority=self.meta.priority
        )


class OpenAICompatibleLLMPlugin(Plugin):
    meta = PluginMeta(
        name="llm.openai_compatible",
        version="0.1.0",
        description="OpenAI-compatible chat completions provider (stdlib HTTP client).",
        provides=("llm",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        settings = dict(context.config)
        model = settings.pop("model", "")
        if not model:
            # Registering without a model is harmless: this provider is not the
            # default and is only usable once configured.
            model = "gpt-4o-mini"
        provider = OpenAICompatibleLLM(
            model=model,
            base_url=settings.pop("base_url", "https://api.openai.com/v1"),
            api_key=settings.pop("api_key", None),
            timeout=float(settings.pop("timeout", 60.0)),
            temperature=float(settings.pop("temperature", 0.2)),
            endpoint=settings.pop("endpoint", None),
            extra_headers=settings.pop("extra_headers", None),
        )
        context.container.register(
            Capability.LLM,
            "openai_compatible",
            provider,
            plugin=self.meta.name,
            priority=self.meta.priority,
            default=False,
        )
