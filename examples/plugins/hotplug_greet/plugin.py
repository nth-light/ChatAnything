"""Example hot-pluggable plugin: registers a greeting tool at runtime."""

from chatanything import Capability, Plugin, PluginContext, PluginMeta
from chatanything.models import ToolSpec


class GreetTool:
    """A minimal tool implementing the framework tool protocol."""

    spec = ToolSpec(
        name="hotplug_greet",
        description="Greet a person by name.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Person name"}},
            "required": ["name"],
        },
    )

    async def invoke(self, arguments, context) -> str:  # noqa: ANN001, ANN201
        name = arguments.get("name", "world")
        return f"Hello, {name}! This tool was hot-loaded."


class GreetPlugin(Plugin):
    meta = PluginMeta(
        name="example.hotplug_greet",
        version="1.0.0",
        description="Hot-plug demo plugin",
        provides=("tool",),
        priority=30,
    )

    def __init__(self) -> None:
        self.tool = GreetTool()

    async def install(self, context: PluginContext) -> None:
        context.container.register(
            Capability.TOOL,
            self.tool.spec.name,
            self.tool,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )

    async def on_initialize(self, context: PluginContext) -> None:
        context.logger.info("hotplug_greet: initialized")

    async def on_start(self, context: PluginContext) -> None:
        context.logger.info("hotplug_greet: started")

    async def on_stop(self, context: PluginContext) -> None:
        context.logger.info("hotplug_greet: stopped")

    async def on_destroy(self, context: PluginContext) -> None:
        context.logger.info("hotplug_greet: destroyed")
