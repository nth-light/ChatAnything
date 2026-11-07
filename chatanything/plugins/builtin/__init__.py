"""Built-in plugin bundle.

Call :func:`builtin_plugins` to obtain fresh plugin instances.  The harness
installs them in this order; later plugins with higher ``priority`` can replace
any default capability.
"""

from __future__ import annotations

from .channels import ConsoleChannelPlugin
from .context import InlineSpillContextPlugin
from .environment import LocalEnvironmentPlugin
from .filters import RuleFilterPlugin, SemanticFilterPlugin
from .llm import EchoLLMPlugin, OpenAICompatibleLLMPlugin
from .memory import InMemoryMemoryPlugin
from .parser import JsonOutputParserPlugin
from .prompts import DefaultPromptPlugin
from .router_policy import LLMRouterPolicyPlugin
from .speculator import NoopSpeculatorPlugin
from .streaming import StreamingHubPlugin
from .tools import CoreToolsPlugin
from .watchdog import StallWatchdogPlugin

from ..base import Plugin
from ...core.loops import ReActAgentLoopPlugin


def builtin_plugins() -> list[Plugin]:
    """Return a fresh list of the framework's default plugins."""

    return [
        InMemoryMemoryPlugin(),
        DefaultPromptPlugin(),
        JsonOutputParserPlugin(),
        CoreToolsPlugin(),
        ConsoleChannelPlugin(),
        EchoLLMPlugin(),
        OpenAICompatibleLLMPlugin(),
        RuleFilterPlugin(),
        SemanticFilterPlugin(),
        InlineSpillContextPlugin(),
        LLMRouterPolicyPlugin(),
        ReActAgentLoopPlugin(),
        StreamingHubPlugin(),
        StallWatchdogPlugin(),
        LocalEnvironmentPlugin(),
        NoopSpeculatorPlugin(),
    ]
