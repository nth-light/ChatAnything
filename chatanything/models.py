"""Domain models shared by the whole harness.

The vocabulary follows the Super-agent design document:

* ``Trigger``  -- a half-structured environmental event (IM message, GitHub
  review, CI result, scheduled tick ...).
* ``Thread``   -- a continuous event stream (one IM topic, one Pull Request,
  one mail thread ...).
* ``Session``  -- a Router / Worker / Watchdog execution context.
* ``Artifact`` -- spilled content that can be recalled by reference id.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


def new_id(prefix: str) -> str:
    """Generate a short, prefixed, globally unique identifier."""

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utc_now() -> float:
    """Return the current UNIX timestamp."""

    return time.time()


class SessionKind(str, Enum):
    """The three logical session types in the harness."""

    ROUTER = "router"
    WORKER = "worker"
    WATCHDOG = "watchdog"


class SessionStatus(str, Enum):
    """Session lifecycle states."""

    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    REPORTED = "reported"
    CLOSED = "closed"
    FAILED = "failed"

    @property
    def active(self) -> bool:
        """Whether the session is doing (or waiting to do) real work."""

        return self in (SessionStatus.CREATED, SessionStatus.RUNNING)

    @property
    def terminal(self) -> bool:
        """Whether the session can no longer make progress."""

        return self in (SessionStatus.CLOSED, SessionStatus.FAILED)


class FilterStage(str, Enum):
    """Fast / slow path of the two-stage trigger filter."""

    FAST = "fast"
    SLOW = "slow"


class RouterAction(str, Enum):
    """What the Router decides to do with a trigger."""

    IGNORE = "ignore"
    REPLY = "reply"
    DISPATCH = "dispatch"
    NOTIFY = "notify"


@dataclass(slots=True)
class ToolCall:
    """A model-issued request to invoke a tool."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("call"))


@dataclass(slots=True)
class ToolResult:
    """The normalized result of a tool invocation."""

    call_id: str
    name: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Message:
    """A single chat message exchanged with a model."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    created_at: float = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def system(cls, content: str, **metadata: Any) -> "Message":
        return cls(role="system", content=content, metadata=metadata)

    @classmethod
    def user(cls, content: str, **metadata: Any) -> "Message":
        return cls(role="user", content=content, metadata=metadata)

    @classmethod
    def assistant(
        cls,
        content: str,
        *,
        tool_calls: list[ToolCall] | None = None,
        **metadata: Any,
    ) -> "Message":
        return cls(
            role="assistant",
            content=content,
            tool_calls=tool_calls or [],
            metadata=metadata,
        )

    @classmethod
    def tool(cls, content: str, *, call_id: str, name: str, **metadata: Any) -> "Message":
        return cls(
            role="tool",
            content=content,
            tool_call_id=call_id,
            name=name,
            metadata=metadata,
        )


@dataclass(slots=True)
class ToolSpec:
    """A declarative description of a tool exposed to the model."""

    name: str
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    background: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ContextReference:
    """A lightweight pointer used by the chain-of-recall mechanism."""

    id: str
    kind: str
    summary: str
    references: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Artifact:
    """Content spilled out of the context window but recallable by id."""

    id: str
    kind: str
    summary: str
    content: str
    references: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Trigger:
    """An event emitted by an external system, a user, or a scheduler."""

    text: str
    source: str = "console"
    channel: str = "console"
    actor: str | None = None
    thread_id: str | None = None
    event_type: str = "message"
    mentions: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("trigger"))
    timestamp: float = field(default_factory=utc_now)
    references: list[str] = field(default_factory=list)
    priority: int = 0

    def brief(self, limit: int = 160) -> str:
        """Return a compact one-line representation for prompts and logs."""

        text = " ".join(self.text.split())
        if len(text) > limit:
            text = text[: limit - 3] + "..."
        return f"[{self.source}/{self.channel}] {self.actor or 'unknown'}: {text}"

    @property
    def ref(self) -> str:
        return f"trigger:{self.id}"


# Friendly alias: the design document calls these objects ``Trigger``.
Event = Trigger


@dataclass(slots=True)
class Thread:
    """A continuous event stream that a Worker can subscribe to."""

    id: str
    source: str
    channel: str
    title: str = ""
    participants: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=utc_now)
    updated_at: float = field(default_factory=utc_now)
    references: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"thread:{self.id}"

    def touch(self) -> None:
        self.updated_at = utc_now()


@dataclass(slots=True)
class ThreadSubscription:
    """A many-to-many subscription between Workers and Threads."""

    worker_id: str
    thread_id: str
    session_id: str
    created_at: float = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Session:
    """A Router, Worker or Watchdog execution context."""

    id: str
    kind: SessionKind
    title: str = ""
    goal: str = ""
    status: SessionStatus = SessionStatus.CREATED
    thread_ids: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    parent_id: str | None = None
    created_at: float = field(default_factory=utc_now)
    updated_at: float = field(default_factory=utc_now)
    last_activity: float = field(default_factory=utc_now)
    steps: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"session:{self.id}"

    def add_message(self, message: Message) -> Message:
        self.messages.append(message)
        self.touch()
        return message

    def touch(self) -> None:
        now = utc_now()
        self.updated_at = now
        self.last_activity = now


@dataclass(slots=True)
class FilterDecision:
    """The verdict produced by one trigger filter."""

    accepted: bool
    reason: str = ""
    stage: FilterStage = FilterStage.FAST
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RouterDecision:
    """The Router's decision for a single trigger."""

    trigger_id: str
    action: RouterAction = RouterAction.IGNORE
    task: str | None = None
    reply: str | None = None
    thread_id: str | None = None
    target_worker: str | None = None
    reason: str = ""
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RouterPlan:
    """The Router's batched plan covering one or more triggers."""

    decisions: list[RouterDecision] = field(default_factory=list)

    def for_trigger(self, trigger_id: str) -> RouterDecision | None:
        for decision in self.decisions:
            if decision.trigger_id == trigger_id:
                return decision
        return None


@dataclass(slots=True)
class ContextBundle:
    """The context assembled for a model call."""

    messages: list[Message] = field(default_factory=list)
    references: list[ContextReference] = field(default_factory=list)
    spilled: list[str] = field(default_factory=list)
    estimated_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMResponse:
    """A normalized model response."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


@dataclass(slots=True)
class LLMStreamChunk:
    """One incremental piece of a streamed model response.

    ``delta`` carries new text for this chunk; the final chunk sets
    ``response`` to the complete normalized response (including tool calls
    and usage).  Providers that cannot stream may omit ``astream`` entirely.
    """

    delta: str = ""
    response: LLMResponse | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedOutput:
    """The result of parsing a model utterance."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_answer: str | None = None

    @property
    def is_final(self) -> bool:
        return self.final_answer is not None and not self.tool_calls


@dataclass(slots=True)
class AgentResult:
    """The outcome of one agent loop run."""

    answer: str
    steps: int = 0
    error: str | None = None
    stopped_reason: str = "completed"
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class WorkerReport:
    """A Worker's conclusion reported back to the Router."""

    session_id: str
    thread_id: str | None
    channel: str
    answer: str
    steps: int = 0
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    created_at: float = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutboundMessage:
    """A message emitted to an external channel."""

    channel: str
    text: str
    thread_id: str | None = None
    session_id: str | None = None
    kind: str = "reply"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WatchdogFinding:
    """A health issue detected by the external control loop."""

    code: str
    message: str
    severity: str = "warning"
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Speculation:
    """A tentative, background result produced before it is needed."""

    trigger_id: str
    thread_id: str | None
    result: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecResult:
    """The output of an execution-environment command."""

    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelConfig:
    """A persisted model backend configuration."""

    name: str
    provider: str = "openai"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    is_default: bool = False
    id: str = field(default_factory=lambda: new_id("model"))
    created_at: float = field(default_factory=utc_now)
    updated_at: float = field(default_factory=utc_now)


@dataclass(slots=True)
class MCPServerConfig:
    """A persisted MCP server connection configuration."""

    name: str
    transport: str = "stdio"
    command: str = ""
    url: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    id: str = field(default_factory=lambda: new_id("mcp"))
    created_at: float = field(default_factory=utc_now)
    updated_at: float = field(default_factory=utc_now)


@dataclass(slots=True)
class MCPEntry:
    """A cached MCP artifact (tool, resource or prompt) discovered on a server."""

    server_id: str
    kind: str
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("mcp_entry"))
    updated_at: float = field(default_factory=utc_now)


@dataclass(slots=True)
class SkillConfig:
    """A persisted skill definition available to agents."""

    name: str
    description: str = ""
    path: str = ""
    version: str = "0.1.0"
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("skill"))
    created_at: float = field(default_factory=utc_now)
    updated_at: float = field(default_factory=utc_now)
