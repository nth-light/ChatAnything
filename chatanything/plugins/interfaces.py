"""Typed contracts for every pluggable capability.

These ``Protocol`` definitions are the public ABI of the harness.  A plugin
implements one or more of them and registers the implementation under the
matching :class:`~chatanything.registry.Capability`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ContextManager, Mapping, Protocol, Sequence

from ..models import (
    AgentResult,
    Artifact,
    ContextBundle,
    ContextReference,
    ExecResult,
    FilterDecision,
    FilterStage,
    LLMResponse,
    MCPServerConfig,
    MCPEntry,
    Message,
    ModelConfig,
    OutboundMessage,
    ParsedOutput,
    RouterPlan,
    Session,
    SkillConfig,
    Speculation,
    Thread,
    ThreadSubscription,
    ToolCall,
    ToolResult,
    ToolSpec,
    Trigger,
    WatchdogFinding,
    WorkerReport,
)

if TYPE_CHECKING:
    from ..runtime import Runtime


@dataclass(slots=True)
class ToolContext:
    """Runtime information available to a tool invocation."""

    runtime: "Runtime"
    session: Session | None = None
    call: ToolCall | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    """A model backend (echo, OpenAI-compatible, local vLLM, ...)."""

    name: str

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        session: Session | None = None,
    ) -> LLMResponse:
        ...


class StreamSubscription(Protocol):
    """A consumer attached to one stream key."""

    key: str

    def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        """Return the next event; raise ``TimeoutError`` when idle."""

    def close(self) -> None:
        """Detach from the broker; safe to call multiple times."""


class StreamBroker(Protocol):
    """Fan-out hub for incremental model output (SSE, WebSocket, ...)."""

    name: str

    def publish(self, key: str, event: Mapping[str, Any]) -> None:
        """Publish one event to every subscriber of ``key``."""

    def subscribe(self, key: str, *, replay: bool = True) -> StreamSubscription:
        """Attach a subscriber, optionally replaying buffered events."""


class EventFilter(Protocol):
    """One stage of the two-stage (Fast Path / Slow Path) filter."""

    name: str
    stage: FilterStage
    priority: int

    async def evaluate(self, trigger: Trigger, runtime: "Runtime") -> FilterDecision:
        ...


class ContextOrganizer(Protocol):
    """Assemble the model context using Inline & Spill + chain of recall."""

    name: str

    async def build(
        self,
        session: Session,
        runtime: "Runtime",
        *,
        pending: Sequence[Trigger] = (),
        budget: int | None = None,
    ) -> ContextBundle:
        ...


class MemoryStore(Protocol):
    """Durable state boundary (in-memory, SQLite, Postgres, remote service)."""

    name: str

    async def save_session(self, session: Session) -> None:
        ...

    async def get_session(self, session_id: str) -> Session | None:
        ...

    async def list_sessions(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[Session]:
        ...

    async def list_messages(
        self, session_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[Message]:
        ...

    async def append_message(self, session_id: str, message: Message) -> None:
        ...

    async def save_thread(self, thread: Thread) -> None:
        ...

    async def get_thread(self, thread_id: str) -> Thread | None:
        ...

    async def list_threads(self) -> list[Thread]:
        ...

    async def save_trigger(self, trigger: Trigger) -> None:
        ...

    async def get_trigger(self, trigger_id: str) -> Trigger | None:
        ...

    async def save_subscription(self, subscription: ThreadSubscription) -> None:
        ...

    async def list_subscriptions(
        self,
        *,
        thread_id: str | None = None,
        worker_id: str | None = None,
    ) -> list[ThreadSubscription]:
        ...

    async def remove_subscription(self, worker_id: str, thread_id: str) -> None:
        ...

    async def save_artifact(self, artifact: Artifact) -> None:
        ...

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        ...

    async def resolve_reference(self, reference_id: str) -> ContextReference | None:
        ...


class RouterPolicy(Protocol):
    """The decision policy used by the Router layer."""

    name: str

    async def decide(
        self,
        triggers: Sequence[Trigger],
        runtime: "Runtime",
        *,
        history: Sequence[Message] = (),
    ) -> RouterPlan:
        ...


class PromptProvider(Protocol):
    """Prompt templates for filtering, routing and worker reasoning."""

    name: str

    def filter_messages(self, trigger: Trigger) -> list[Message]:
        ...

    def router_messages(
        self,
        triggers: Sequence[Trigger],
        history: Sequence[Message],
        *,
        agent_name: str,
    ) -> list[Message]:
        ...

    def worker_messages(self, session: Session, goal: str) -> list[Message]:
        ...


class OutputParser(Protocol):
    """Parse model output into tool calls or a final answer."""

    name: str

    def parse(self, text: str) -> ParsedOutput:
        ...


class Tool(Protocol):
    """A callable capability exposed to the model."""

    spec: ToolSpec

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> str | ToolResult:
        ...


class Channel(Protocol):
    """A human-facing communication channel (IM, GitHub, mail, console...)."""

    name: str

    async def send(self, message: OutboundMessage) -> None:
        ...


class AgentLoop(Protocol):
    """The reasoning loop used inside a Worker session."""

    name: str

    async def run(self, session: Session, goal: str, runtime: "Runtime") -> AgentResult:
        ...


class Watchdog(Protocol):
    """An external control loop that detects and repairs abnormal runtime state."""

    name: str

    async def check(self, runtime: "Runtime") -> list[WatchdogFinding]:
        ...

    async def heal(self, finding: WatchdogFinding, runtime: "Runtime") -> bool:
        ...


class EnvironmentProvider(Protocol):
    """An execution environment decoupled from compute and state."""

    name: str

    async def describe(self) -> dict[str, Any]:
        ...

    async def execute(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> ExecResult:
        ...


class Speculator(Protocol):
    """Optional speculative-execution policy.

    Implementations may run work in the background before the Router decides
    whether it is needed.  Results are discarded for ignored triggers.
    """

    name: str

    async def prepare(self, trigger: Trigger, runtime: "Runtime") -> Speculation | None:
        ...

    async def evaluate(
        self,
        trigger: Trigger,
        speculation: Speculation,
        runtime: "Runtime",
    ) -> bool:
        ...


class EventSource(Protocol):
    """A long-running source of external triggers (IM bot, webhook, scheduler)."""

    name: str

    async def start(self, emit: Callable[[Trigger], Awaitable[None]]) -> None:
        ...

    async def stop(self) -> None:
        ...


async def maybe_await(value: Any) -> Any:
    """Await a value if it is awaitable; return it unchanged otherwise."""

    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


def normalize_tool_result(result: str | ToolResult, call: ToolCall) -> ToolResult:
    """Normalize a tool's return value into :class:`ToolResult`."""

    if isinstance(result, ToolResult):
        return result
    return ToolResult(call_id=call.id, name=call.name, content=str(result))


class ModelConfigStore(Protocol):
    """Durable storage for model backend configurations."""

    name: str

    async def save_model_config(self, config: ModelConfig) -> ModelConfig:
        ...

    async def get_model_config(self, config_id: str) -> ModelConfig | None:
        ...

    async def list_model_configs(self, *, enabled: bool | None = None) -> list[ModelConfig]:
        ...

    async def delete_model_config(self, config_id: str) -> bool:
        ...

    async def set_default_model(self, config_id: str) -> None:
        ...

    async def get_default_model(self) -> ModelConfig | None:
        ...


class MCPStore(Protocol):
    """Durable storage for MCP servers and their discovered entries."""

    name: str

    async def save_server(self, server: MCPServerConfig) -> MCPServerConfig:
        ...

    async def get_server(self, server_id: str) -> MCPServerConfig | None:
        ...

    async def list_servers(self, *, enabled: bool | None = None) -> list[MCPServerConfig]:
        ...

    async def delete_server(self, server_id: str) -> bool:
        ...

    async def upsert_entry(self, entry: MCPEntry) -> MCPEntry:
        ...

    async def list_entries(self, server_id: str, *, kind: str | None = None) -> list[MCPEntry]:
        ...

    async def delete_entry(self, server_id: str, kind: str, name: str) -> bool:
        ...


class SkillRegistry(Protocol):
    """Durable registry of skill definitions."""

    name: str

    async def save_skill(self, skill: SkillConfig) -> SkillConfig:
        ...

    async def get_skill(self, skill_id: str) -> SkillConfig | None:
        ...

    async def list_skills(
        self, *, enabled: bool | None = None, tag: str | None = None
    ) -> list[SkillConfig]:
        ...

    async def delete_skill(self, skill_id: str) -> bool:
        ...
