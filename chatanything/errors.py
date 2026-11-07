"""Exception hierarchy for ChatAnything."""

from __future__ import annotations


class ChatAnythingError(Exception):
    """Base class for all framework errors."""


class InvalidTransitionError(ChatAnythingError):
    """Raised when an illegal lifecycle transition is requested."""


class ComponentNotFoundError(ChatAnythingError):
    """Raised when a capability cannot be resolved from the registry."""

    def __init__(self, slot: str, name: str | None = None) -> None:
        target = f"{slot}:{name}" if name else slot
        super().__init__(f"component not found: {target}")
        self.slot = slot
        self.name = name


class DuplicateComponentError(ChatAnythingError):
    """Raised when registering a component name that already exists."""

    def __init__(self, slot: str, name: str) -> None:
        super().__init__(f"component already registered: {slot}:{name}")
        self.slot = slot
        self.name = name


class PluginError(ChatAnythingError):
    """Base class for plugin related errors."""


class PluginDependencyError(PluginError):
    """Raised when a plugin dependency has not been installed."""


class PluginLoadError(PluginError):
    """Raised when a plugin cannot be imported or instantiated."""


class PluginSourceError(PluginLoadError):
    """Raised when a plugin source cannot be read, downloaded or extracted."""


class PluginValidationError(PluginError):
    """Raised when a plugin manifest, package or plugin class is invalid."""


class PluginStateError(PluginError):
    """Raised when an operation is invalid for the current plugin state."""


class PluginVersionConflictError(PluginDependencyError):
    """Raised when plugin versions cannot satisfy the declared constraints."""


class PluginDependencyCycleError(PluginDependencyError):
    """Raised when plugins form a circular dependency graph."""


class PluginSandboxError(PluginError):
    """Raised when a sandbox policy or timeout is violated."""


class PluginRollbackError(PluginError):
    """Raised when a failed hot-plug operation could not be rolled back cleanly."""


class ConfigurationError(ChatAnythingError):
    """Raised when the harness configuration is invalid."""


class ToolExecutionError(ChatAnythingError):
    """Raised when a tool fails in an unrecoverable way."""


class EnvironmentExecutionError(ChatAnythingError):
    """Raised when an execution environment refuses or fails a command."""
