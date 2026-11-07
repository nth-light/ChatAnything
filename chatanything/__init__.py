"""ChatAnything -- a lifecycle-driven, everything-is-a-plugin Agent harness.

Quick start::

    import asyncio
    from chatanything import Harness

    async def main():
        async with Harness() as harness:
            await harness.submit("请帮我计算 1 + 1")
            await harness.wait_until_idle()

    asyncio.run(main())
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import HarnessConfig
from .core.harness import Harness, create_harness
from .errors import (
    ChatAnythingError,
    ComponentNotFoundError,
    ConfigurationError,
    InvalidTransitionError,
    PluginDependencyCycleError,
    PluginDependencyError,
    PluginError,
    PluginLoadError,
    PluginRollbackError,
    PluginSandboxError,
    PluginSourceError,
    PluginStateError,
    PluginValidationError,
    PluginVersionConflictError,
    ToolExecutionError,
)
from .lifecycle import LifecycleEngine, LifecycleEvent, LifecyclePoint, LifecycleState
from .models import (
    AgentResult,
    Artifact,
    ContextBundle,
    ContextReference,
    FilterDecision,
    FilterStage,
    LLMResponse,
    MCPServerConfig,
    MCPEntry,
    Message,
    ModelConfig,
    OutboundMessage,
    ParsedOutput,
    RouterAction,
    RouterDecision,
    RouterPlan,
    Session,
    SessionKind,
    SessionStatus,
    SkillConfig,
    Thread,
    ThreadSubscription,
    ToolCall,
    ToolResult,
    ToolSpec,
    Trigger,
    WorkerReport,
    new_id,
)
from .plugins.base import PLUGIN_API_VERSION, Plugin, PluginContext, PluginMeta
from .plugins.builtin import builtin_plugins
from .plugins.interfaces import (
    AgentLoop,
    Channel,
    ContextOrganizer,
    EnvironmentProvider,
    EventFilter,
    EventSource,
    LLMProvider,
    MCPStore,
    MemoryStore,
    ModelConfigStore,
    OutputParser,
    PromptProvider,
    RouterPolicy,
    SkillRegistry,
    Speculator,
    Tool,
    ToolContext,
    Watchdog,
)
from .plugins.hotplug import (
    ArchiveSource,
    DirectorySource,
    PluginManifest,
    PluginMetrics,
    PluginSandbox,
    PluginSource,
    PluginState,
    PluginStatus,
    RemoteSource,
    Requirement,
    SandboxPolicy,
    StagedPackage,
    Version,
)
from .plugins.manager import InstalledPlugin, PluginManager
from .registry import Capability, ComponentEntry, ComponentRegistry

__all__ = [
    "__version__",
    # Harness
    "Harness",
    "HarnessConfig",
    "create_harness",
    # Lifecycle
    "LifecycleEngine",
    "LifecycleEvent",
    "LifecyclePoint",
    "LifecycleState",
    # Plugins
    "Plugin",
    "PluginContext",
    "PluginMeta",
    "PluginManager",
    "InstalledPlugin",
    "PLUGIN_API_VERSION",
    "PluginManifest",
    "PluginMetrics",
    "PluginState",
    "PluginStatus",
    "PluginSandbox",
    "SandboxPolicy",
    "PluginSource",
    "DirectorySource",
    "ArchiveSource",
    "RemoteSource",
    "StagedPackage",
    "Requirement",
    "Version",
    "builtin_plugins",
    "Capability",
    "ComponentEntry",
    "ComponentRegistry",
    # Contracts
    "AgentLoop",
    "Channel",
    "ContextOrganizer",
    "EnvironmentProvider",
    "EventFilter",
    "EventSource",
    "LLMProvider",
    "MCPStore",
    "MemoryStore",
    "ModelConfigStore",
    "OutputParser",
    "PromptProvider",
    "RouterPolicy",
    "SkillRegistry",
    "Speculator",
    "Tool",
    "ToolContext",
    "Watchdog",
    # Models
    "AgentResult",
    "Artifact",
    "ContextBundle",
    "ContextReference",
    "FilterDecision",
    "FilterStage",
    "LLMResponse",
    "MCPServerConfig",
    "MCPEntry",
    "Message",
    "ModelConfig",
    "OutboundMessage",
    "ParsedOutput",
    "RouterAction",
    "RouterDecision",
    "RouterPlan",
    "Session",
    "SessionKind",
    "SessionStatus",
    "SkillConfig",
    "Thread",
    "ThreadSubscription",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Trigger",
    "WorkerReport",
    "new_id",
    # Errors
    "ChatAnythingError",
    "ComponentNotFoundError",
    "ConfigurationError",
    "InvalidTransitionError",
    "PluginDependencyError",
    "PluginDependencyCycleError",
    "PluginError",
    "PluginLoadError",
    "PluginRollbackError",
    "PluginSandboxError",
    "PluginSourceError",
    "PluginStateError",
    "PluginValidationError",
    "PluginVersionConflictError",
    "ToolExecutionError",
]
