"""REST API and static-file routing for the ChatAnything web UI.

:class:`WebApplication` runs inside the HTTP server thread while the harness
itself lives on an asyncio event loop.  Every call into the store or the
harness is therefore bridged with
``asyncio.run_coroutine_threadsafe(...).result()``.

All API responses are UTF-8 JSON and never leak tracebacks to the client.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Iterator, Mapping, TypeVar
from urllib.parse import parse_qs, unquote

from ..core.streaming import stream_key
from ..lifecycle import LifecycleEngine
from ..models import (
    MCPServerConfig,
    MCPEntry,
    Message,
    ModelConfig,
    Session,
    SkillConfig,
    ToolCall,
    Trigger,
)
from ..plugins.interfaces import ToolContext, normalize_tool_result
from ..registry import Capability, ComponentRegistry

T = TypeVar("T")

_LOGGER = logging.getLogger("chatanything.web")

_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

_API_HEADERS: dict[str, str] = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}

_MCP_ENTRY_KINDS: frozenset[str] = frozenset({"tool", "resource", "prompt"})

_SSE_HEADERS: dict[str, str] = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",
}

_TERMINAL_EVENTS: frozenset[str] = frozenset({"done", "error"})


class _NotFound(Exception):
    """Raised when a route or resource does not exist."""


class _MethodNotAllowed(Exception):
    """Raised when a known path is called with an unsupported method."""


class _ServiceUnavailable(Exception):
    """Raised when a capability needed for the response is not installed."""


def _sse_frame(event: Mapping[str, Any]) -> bytes:
    """Encode one event as an SSE ``data:`` frame."""

    payload = json.dumps(event, ensure_ascii=False, default=str)
    return f"data: {payload}\n\n".encode("utf-8")


# -------------------------------------------------------------------------#
# Parsing / serialization helpers
# -------------------------------------------------------------------------#


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def _json_response(status: int, payload: Any) -> tuple[int, dict[str, str], bytes]:
    return status, dict(_API_HEADERS), _json_bytes(payload)


def _json_error(status: int, message: str) -> tuple[int, dict[str, str], bytes]:
    return _json_response(status, {"error": message})


def _segments(path: str) -> list[str]:
    raw = path.strip("/")
    if not raw:
        return []
    return [unquote(segment) for segment in raw.split("/")]


def _query_params(query: str) -> dict[str, str]:
    parsed = parse_qs(query, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _parse_bool(value: Any, *, default: bool | None = None) -> bool:
    if value is None:
        if default is None:
            raise ValueError("boolean value is required")
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _parse_int(value: Any, *, default: int, minimum: int = 0) -> int:
    if value is None:
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"invalid integer value: {value!r}") from None
    return max(minimum, number)


def _parse_float(value: Any, *, default: float, minimum: float = 0.1) -> float:
    if value is None:
        return default
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"invalid number value: {value!r}") from None
    return max(minimum, number)


def _optional_bool(params: Mapping[str, str], key: str) -> bool | None:
    if key not in params:
        return None
    return _parse_bool(params[key])


def _json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("request body is not valid UTF-8") from None
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc.msg}") from None
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _optional_str(data: Mapping[str, Any], key: str, default: str) -> str:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _object_value(value: Any, key: str, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if value is None:
        return dict(default or {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return dict(value)


def _list_value(value: Any, key: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    return list(value)


def _string_list(value: Any, key: str) -> list[str]:
    return [str(item) for item in _list_value(value, key)]


def _string_map(value: Any, key: str) -> dict[str, str]:
    return {str(item_key): str(item_value) for item_key, item_value in _object_value(value, key).items()}


def _message_payload(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": [
            {"name": call.name, "arguments": call.arguments, "id": call.id}
            for call in message.tool_calls
        ],
        "created_at": message.created_at,
        "metadata": message.metadata,
    }


def _session_summary(session: Session) -> dict[str, Any]:
    """Session view without the message history, plus a message count."""

    return {
        "id": session.id,
        "kind": session.kind.value,
        "title": session.title,
        "goal": session.goal,
        "status": session.status.value,
        "thread_ids": list(session.thread_ids),
        "references": list(session.references),
        "parent_id": session.parent_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "last_activity": session.last_activity,
        "steps": session.steps,
        "metadata": session.metadata,
        "message_count": len(session.messages),
    }


def _session_detail(session: Session) -> dict[str, Any]:
    payload = _session_summary(session)
    payload["messages"] = [_message_payload(message) for message in session.messages]
    return payload


async def _await_value(awaitable: Awaitable[T]) -> T:
    return await awaitable


# -------------------------------------------------------------------------#
# Application
# -------------------------------------------------------------------------#


class WebApplication:
    """Synchronous HTTP-facing facade over the async harness and stores."""

    def __init__(
        self,
        container: ComponentRegistry,
        lifecycle: LifecycleEngine,
        emit: Callable[[Trigger], Awaitable[Any]],
        loop: asyncio.AbstractEventLoop,
        static_dir: Path,
        *,
        request_timeout: float = 30.0,
    ) -> None:
        self.container = container
        self.lifecycle = lifecycle
        self.emit = emit
        self.loop = loop
        self.static_dir = Path(static_dir)
        self.request_timeout = request_timeout

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def dispatch(
        self,
        method: str,
        path: str,
        query: str,
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        """Route one HTTP request; errors are mapped to JSON responses."""

        method = method.upper()
        try:
            if not path.startswith("/api/"):
                return self._static_response(path)
            status, payload = self._route_api(method, path, query, body)
            return _json_response(status, payload)
        except _NotFound as exc:
            return _json_error(404, str(exc) or "not found")
        except _MethodNotAllowed:
            return _json_error(405, "method not allowed")
        except ValueError as exc:
            return _json_error(400, str(exc) or "bad request")
        except Exception:  # noqa: BLE001 - never leak internals to the client
            _LOGGER.exception("request failed: %s %s", method, path)
            return _json_error(500, "internal server error")

    # ------------------------------------------------------------------
    # Server-sent events
    # ------------------------------------------------------------------
    def open_stream(self, query: str) -> tuple[int, dict[str, str], Iterator[bytes]]:
        """Open the SSE event stream for one accepted trigger.

        Returns ``(status, headers, frames)`` where ``frames`` is a blocking
        iterator of already-encoded SSE frames.  The generator detaches from
        the broker when the client disconnects.
        """

        params = _query_params(query)
        trigger_id = str(params.get("trigger_id", "")).strip()
        if not trigger_id:
            raise ValueError("trigger_id is required")
        heartbeat = _parse_float(params.get("heartbeat"), default=15.0)
        timeout = _parse_float(params.get("timeout"), default=300.0)

        try:
            broker = self.container.resolve(Capability.STREAM)
        except Exception:  # noqa: BLE001 - a missing hub is a deployment issue
            raise _ServiceUnavailable("stream broker is not installed") from None

        subscription = broker.subscribe(stream_key(trigger_id))
        frames = self._sse_frames(subscription, heartbeat=heartbeat, timeout=timeout)
        return 200, dict(_SSE_HEADERS), frames

    def _sse_frames(
        self,
        subscription: Any,
        *,
        heartbeat: float,
        timeout: float,
    ) -> Iterator[bytes]:
        deadline = time.monotonic() + timeout
        try:
            yield b": connected\n\n"
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield _sse_frame(
                        {
                            "type": "error",
                            "message": "stream timed out",
                            "stopped_reason": "timeout",
                        }
                    )
                    break
                try:
                    event = subscription.next_event(timeout=min(heartbeat, remaining))
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                yield _sse_frame(event)
                if event.get("type") in _TERMINAL_EVENTS:
                    break
        finally:
            subscription.close()

    # ------------------------------------------------------------------
    # Static files
    # ------------------------------------------------------------------
    def _static_response(self, path: str) -> tuple[int, dict[str, str], bytes]:
        if path in ("/", "/index.html"):
            relative = "index.html"
        else:
            relative = unquote(path.lstrip("/"))
            if not relative:
                relative = "index.html"

        root = self.static_dir.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise _NotFound("not found") from None
        if not target.is_file():
            raise _NotFound("not found")

        content_type = _MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return 200, {"Content-Type": content_type}, target.read_bytes()

    # ------------------------------------------------------------------
    # API routing
    # ------------------------------------------------------------------
    def _route_api(
        self,
        method: str,
        path: str,
        query: str,
        body: bytes,
    ) -> tuple[int, Any]:
        segments = _segments(path)
        if len(segments) < 2 or segments[0] != "api":
            raise _NotFound("not found")

        head = segments[1]
        rest = segments[2:]
        params = _query_params(query)

        if head == "health" and not rest:
            if method != "GET":
                raise _MethodNotAllowed
            return 200, self._health_payload()
        if head == "chat" and not rest:
            if method != "POST":
                raise _MethodNotAllowed
            return self._chat(body)
        if head == "sessions":
            return self._sessions(method, rest, params)
        if head == "models":
            return self._models(method, rest, params, body)
        if head == "mcp":
            return self._mcp(method, rest, params, body)
        if head == "skills":
            return self._skills(method, rest, params, body)
        if head == "tools":
            return self._tools(method, rest, body)
        raise _NotFound("not found")

    # ------------------------------------------------------------------
    # Health / chat
    # ------------------------------------------------------------------
    def _health_payload(self) -> dict[str, Any]:
        store: dict[str, Any] = {}
        try:
            memory = self.container.resolve(Capability.MEMORY)
        except Exception:  # noqa: BLE001 - health must stay available
            memory = None
        if memory is not None and hasattr(memory, "snapshot"):
            try:
                store = memory.snapshot()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("memory snapshot failed")
                store = {}

        plugins: set[str] = set()
        for slot in Capability:
            for entry in self.container.entries(slot):
                if entry.plugin:
                    plugins.add(entry.plugin)

        tools = [
            tool.spec.name for tool in self.container.resolve_all(Capability.TOOL)
        ]
        return {
            "status": "ok",
            "state": self.lifecycle.state.value,
            "capabilities": self.container.snapshot(),
            "plugins": sorted(plugins),
            "store": store,
            "tools": tools,
        }

    def _chat(self, body: bytes) -> tuple[int, Any]:
        data = _json_body(body)
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        actor_value = data.get("actor")
        actor = actor_value if isinstance(actor_value, str) and actor_value else "web"
        thread_value = data.get("thread_id")
        thread_id = thread_value if isinstance(thread_value, str) and thread_value else None

        trigger = Trigger(
            text=text,
            source="web",
            channel="web",
            actor=actor,
            thread_id=thread_id,
            metadata={"via": "web"},
        )
        decision = self._call(self.emit(trigger))
        return 200, {
            "trigger_id": trigger.id,
            "accepted": bool(getattr(decision, "accepted", False)),
            "reason": str(getattr(decision, "reason", "")),
            "thread_id": trigger.thread_id,
        }

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    def _sessions(
        self,
        method: str,
        rest: list[str],
        params: Mapping[str, str],
    ) -> tuple[int, Any]:
        if method != "GET":
            raise _MethodNotAllowed
        memory = self.container.resolve(Capability.MEMORY)

        if not rest:
            kind = params.get("kind") or None
            status = params.get("status") or None
            limit = _parse_int(params.get("limit"), default=50)
            sessions = self._call(memory.list_sessions(kind=kind, status=status))
            return 200, [_session_summary(session) for session in sessions[:limit]]

        session_id = rest[0]
        if len(rest) == 1:
            session = self._call(memory.get_session(session_id))
            if session is None:
                raise _NotFound(f"session not found: {session_id}")
            return 200, _session_detail(session)

        if len(rest) == 2 and rest[1] == "messages":
            session = self._call(memory.get_session(session_id))
            if session is None:
                raise _NotFound(f"session not found: {session_id}")
            limit = _parse_int(params.get("limit"), default=100)
            offset = _parse_int(params.get("offset"), default=0)
            if hasattr(memory, "list_messages"):
                messages = self._call(
                    memory.list_messages(session_id, limit=limit, offset=offset)
                )
            else:
                messages = session.messages[offset : offset + limit]
            return 200, {
                "session_id": session_id,
                "messages": [_message_payload(message) for message in messages],
                "total": len(session.messages),
            }

        raise _NotFound("not found")

    # ------------------------------------------------------------------
    # Model configs
    # ------------------------------------------------------------------
    def _models(
        self,
        method: str,
        rest: list[str],
        params: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, Any]:
        store = self.container.resolve(Capability.CONFIG_STORE)

        if not rest:
            if method == "GET":
                enabled = _optional_bool(params, "enabled")
                configs = self._call(store.list_model_configs(enabled=enabled))
                return 200, [asdict(config) for config in configs]
            if method == "POST":
                return self._create_model(store, body)
            raise _MethodNotAllowed

        config_id = rest[0]
        if len(rest) == 2 and rest[1] == "default":
            if method != "POST":
                raise _MethodNotAllowed
            config = self._require_model(store, config_id)
            self._call(store.set_default_model(config_id))
            refreshed = self._call(store.get_model_config(config_id))
            return 200, asdict(refreshed or config)

        if len(rest) != 1:
            raise _NotFound("not found")
        if method == "GET":
            return 200, asdict(self._require_model(store, config_id))
        if method == "PUT":
            config = self._require_model(store, config_id)
            updates = self._model_updates(_json_body(body))
            updated = replace(config, **updates)
            return 200, asdict(self._call(store.save_model_config(updated)))
        if method == "DELETE":
            if not self._call(store.delete_model_config(config_id)):
                raise _NotFound(f"model not found: {config_id}")
            return 200, {"deleted": True}
        raise _MethodNotAllowed

    def _create_model(self, store: Any, body: bytes) -> tuple[int, Any]:
        data = _json_body(body)
        config = ModelConfig(
            name=_require_str(data, "name"),
            provider=_optional_str(data, "provider", "openai"),
            model=_optional_str(data, "model", ""),
            base_url=_optional_str(data, "base_url", ""),
            api_key=_optional_str(data, "api_key", ""),
            params=_object_value(data.get("params"), "params"),
            enabled=_parse_bool(data.get("enabled"), default=True),
            is_default=_parse_bool(data.get("is_default"), default=False),
        )
        return 201, asdict(self._call(store.save_model_config(config)))

    def _require_model(self, store: Any, config_id: str) -> ModelConfig:
        config = self._call(store.get_model_config(config_id))
        if config is None:
            raise _NotFound(f"model not found: {config_id}")
        return config

    @staticmethod
    def _model_updates(data: Mapping[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if "name" in data:
            updates["name"] = _require_str(data, "name")
        for key in ("provider", "model", "base_url", "api_key"):
            if key in data:
                updates[key] = _optional_str(data, key, "")
        if "params" in data:
            updates["params"] = _object_value(data.get("params"), "params")
        if "enabled" in data:
            updates["enabled"] = _parse_bool(data["enabled"])
        if "is_default" in data:
            updates["is_default"] = _parse_bool(data["is_default"])
        return updates

    # ------------------------------------------------------------------
    # MCP
    # ------------------------------------------------------------------
    def _mcp(
        self,
        method: str,
        rest: list[str],
        params: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, Any]:
        store = self.container.resolve(Capability.MCP)
        if not rest or rest[0] != "servers":
            raise _NotFound("not found")

        if len(rest) == 1:
            if method == "GET":
                enabled = _optional_bool(params, "enabled")
                servers = self._call(store.list_servers(enabled=enabled))
                return 200, [asdict(server) for server in servers]
            if method == "POST":
                return self._create_mcp_server(store, body)
            raise _MethodNotAllowed

        server_id = rest[1]
        if len(rest) == 2:
            if method == "GET":
                return 200, asdict(self._require_mcp_server(store, server_id))
            if method == "PUT":
                server = self._require_mcp_server(store, server_id)
                updates = self._mcp_updates(_json_body(body))
                updated = replace(server, **updates)
                return 200, asdict(self._call(store.save_server(updated)))
            if method == "DELETE":
                if not self._call(store.delete_server(server_id)):
                    raise _NotFound(f"mcp server not found: {server_id}")
                return 200, {"deleted": True}
            raise _MethodNotAllowed

        if len(rest) == 3 and rest[2] == "entries":
            if method == "GET":
                kind = params.get("kind") or None
                entries = self._call(store.list_entries(server_id, kind=kind))
                return 200, [asdict(entry) for entry in entries]
            if method == "PUT":
                return self._upsert_mcp_entry(store, server_id, body)
            raise _MethodNotAllowed

        if len(rest) == 5 and rest[2] == "entries":
            if method != "DELETE":
                raise _MethodNotAllowed
            if not self._call(store.delete_entry(server_id, rest[3], rest[4])):
                raise _NotFound("mcp entry not found")
            return 200, {"deleted": True}

        raise _NotFound("not found")

    def _create_mcp_server(self, store: Any, body: bytes) -> tuple[int, Any]:
        data = _json_body(body)
        server = MCPServerConfig(
            name=_require_str(data, "name"),
            transport=_optional_str(data, "transport", "stdio"),
            command=_optional_str(data, "command", ""),
            url=_optional_str(data, "url", ""),
            args=_string_list(data.get("args"), "args"),
            env=_string_map(data.get("env"), "env"),
            enabled=_parse_bool(data.get("enabled"), default=True),
        )
        return 201, asdict(self._call(store.save_server(server)))

    def _require_mcp_server(self, store: Any, server_id: str) -> MCPServerConfig:
        server = self._call(store.get_server(server_id))
        if server is None:
            raise _NotFound(f"mcp server not found: {server_id}")
        return server

    @staticmethod
    def _mcp_updates(data: Mapping[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if "name" in data:
            updates["name"] = _require_str(data, "name")
        for key in ("transport", "command", "url"):
            if key in data:
                updates[key] = _optional_str(data, key, "")
        if "args" in data:
            updates["args"] = _string_list(data.get("args"), "args")
        if "env" in data:
            updates["env"] = _string_map(data.get("env"), "env")
        if "enabled" in data:
            updates["enabled"] = _parse_bool(data["enabled"])
        return updates

    def _upsert_mcp_entry(self, store: Any, server_id: str, body: bytes) -> tuple[int, Any]:
        data = _json_body(body)
        kind = _require_str(data, "kind")
        if kind not in _MCP_ENTRY_KINDS:
            raise ValueError("kind must be one of: tool, resource, prompt")
        entry = MCPEntry(
            server_id=server_id,
            kind=kind,
            name=_require_str(data, "name"),
            payload=_object_value(data.get("payload"), "payload"),
        )
        return 200, asdict(self._call(store.upsert_entry(entry)))

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------
    def _skills(
        self,
        method: str,
        rest: list[str],
        params: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, Any]:
        store = self.container.resolve(Capability.SKILL)

        if not rest:
            if method == "GET":
                enabled = _optional_bool(params, "enabled")
                tag = params.get("tag") or None
                skills = self._call(store.list_skills(enabled=enabled, tag=tag))
                return 200, [asdict(skill) for skill in skills]
            if method == "POST":
                return self._create_skill(store, body)
            raise _MethodNotAllowed

        skill_id = rest[0]
        if len(rest) != 1:
            raise _NotFound("not found")
        if method == "GET":
            return 200, asdict(self._require_skill(store, skill_id))
        if method == "PUT":
            skill = self._require_skill(store, skill_id)
            updates = self._skill_updates(_json_body(body))
            updated = replace(skill, **updates)
            return 200, asdict(self._call(store.save_skill(updated)))
        if method == "DELETE":
            if not self._call(store.delete_skill(skill_id)):
                raise _NotFound(f"skill not found: {skill_id}")
            return 200, {"deleted": True}
        raise _MethodNotAllowed

    def _create_skill(self, store: Any, body: bytes) -> tuple[int, Any]:
        data = _json_body(body)
        skill = SkillConfig(
            name=_require_str(data, "name"),
            description=_optional_str(data, "description", ""),
            path=_optional_str(data, "path", ""),
            version=_optional_str(data, "version", "0.1.0"),
            enabled=_parse_bool(data.get("enabled"), default=True),
            tags=_string_list(data.get("tags"), "tags"),
            metadata=_object_value(data.get("metadata"), "metadata"),
        )
        return 201, asdict(self._call(store.save_skill(skill)))

    def _require_skill(self, store: Any, skill_id: str) -> SkillConfig:
        skill = self._call(store.get_skill(skill_id))
        if skill is None:
            raise _NotFound(f"skill not found: {skill_id}")
        return skill

    @staticmethod
    def _skill_updates(data: Mapping[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if "name" in data:
            updates["name"] = _require_str(data, "name")
        for key in ("description", "path"):
            if key in data:
                updates[key] = _optional_str(data, key, "")
        if "version" in data:
            updates["version"] = _optional_str(data, "version", "0.1.0")
        if "enabled" in data:
            updates["enabled"] = _parse_bool(data["enabled"])
        if "tags" in data:
            updates["tags"] = _string_list(data.get("tags"), "tags")
        if "metadata" in data:
            updates["metadata"] = _object_value(data.get("metadata"), "metadata")
        return updates

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def _tools(self, method: str, rest: list[str], body: bytes) -> tuple[int, Any]:
        if not rest:
            if method != "GET":
                raise _MethodNotAllowed
            return 200, [
                {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": tool.spec.parameters,
                }
                for tool in self.container.resolve_all(Capability.TOOL)
            ]

        if len(rest) == 2 and rest[1] == "invoke":
            if method != "POST":
                raise _MethodNotAllowed
            return self._invoke_tool(rest[0], body)

        raise _NotFound("not found")

    def _invoke_tool(self, name: str, body: bytes) -> tuple[int, Any]:
        try:
            tool = self.container.resolve(Capability.TOOL, name)
        except Exception:  # noqa: BLE001 - surface as 404
            raise _NotFound(f"tool not found: {name}") from None

        data = _json_body(body)
        arguments = _object_value(data.get("arguments"), "arguments")
        call = ToolCall(name=name, arguments=arguments)
        context = ToolContext(
            runtime=None,  # type: ignore[arg-type]
            call=call,
            metadata={"source": "web"},
        )
        try:
            raw = self._call(self._invoke(tool, arguments, context))
        except Exception as exc:  # noqa: BLE001 - tool failures are data, not crashes
            _LOGGER.debug("tool %s failed: %s", name, exc)
            return 200, {
                "call_id": call.id,
                "name": name,
                "content": f"tool failed: {exc}",
                "is_error": True,
                "metadata": {},
            }

        result = normalize_tool_result(raw, call)
        return 200, {
            "call_id": result.call_id,
            "name": result.name,
            "content": result.content,
            "is_error": result.is_error,
            "metadata": result.metadata,
        }

    @staticmethod
    async def _invoke(tool: Any, arguments: Mapping[str, Any], context: ToolContext) -> Any:
        result = tool.invoke(arguments, context)
        if inspect.isawaitable(result):
            return await result
        return result

    # ------------------------------------------------------------------
    # Async bridge
    # ------------------------------------------------------------------
    def _call(self, awaitable: Awaitable[T]) -> T:
        """Run a coroutine on the harness loop and wait for the result."""

        if self.loop.is_closed():
            raise RuntimeError("event loop is closed")
        coroutine: Coroutine[Any, Any, T]
        if inspect.iscoroutine(awaitable):
            coroutine = awaitable
        else:
            coroutine = _await_value(awaitable)
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=self.request_timeout)
        except FutureTimeoutError:
            future.cancel()
            raise RuntimeError("request timed out") from None


__all__ = ["WebApplication"]
