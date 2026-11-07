# 插件开发规范

[← 返回 README](../README.md)

## 必须遵守

1. **只通过公开契约扩展**：插件实现 `chatanything.plugins.interfaces` 中的 `Protocol`，并向 `Capability` 槽位注册；不要修改或 import 框架内部私有实现。
2. **元数据必须准确**：`meta.name` 必须唯一；`provides` 声明贡献的槽位；`requires` 声明依赖的插件名。
3. **安装幂等、卸载干净**：`install` 中完成注册；外部连接、后台任务等在 `uninstall` 中释放。注册表组件由管理器自动清理。
4. **配置来自 `PluginContext`**：不要读取全局环境或硬编码；使用 `context.setting()`，便于部署时覆盖。
5. **Hook 要短小且容错**：Hook 是旁路观察/注入点，不应阻塞主流程；异常应自行处理或依赖引擎隔离。
6. **运行时错误局部化**：工具与 Worker 的错误应转为 `ToolResult` / `WorkerReport`，避免拖垮整个 Harness。
7. **优先级语义**：Hook 的 `priority` 数值越小越先执行；插件 `meta.priority` 影响安装顺序与同槽位组件的默认排序。

## 示例一：生命周期审计插件

```python
from chatanything import LifecyclePoint, Plugin, PluginMeta


class AuditPlugin(Plugin):
    meta = PluginMeta(
        name="my.audit",
        version="0.1.0",
        description="Audit tool calls and worker reports.",
        priority=20,
    )

    async def install(self, context) -> None:
        self.entries: list[str] = []

    def hooks(self):
        return {
            LifecyclePoint.TOOL_CALL_FINISHED: self.on_tool,
            LifecyclePoint.SESSION_REPORTED: self.on_report,
        }

    async def on_tool(self, event) -> None:
        result = event.payload["result"]
        self.entries.append(f"tool {result.name} -> {result.content}")

    async def on_report(self, event) -> None:
        report = event.payload["report"]
        print(f"[audit] {report.session_id} done in {report.steps} step(s)")
```

## 示例二：替换 LLM 实现

```python
from chatanything import Capability, LLMProvider, LLMResponse, Message, Plugin, PluginMeta, ToolSpec


class MyLLM:
    name = "my_llm"

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] = (),
        session=None,
    ) -> LLMResponse:
        return LLMResponse(content="(my model response)")


class MyLLMPlugin(Plugin):
    meta = PluginMeta(
        name="my.llm",
        version="0.1.0",
        provides=("llm",),
        priority=10,
    )

    async def install(self, context) -> None:
        context.container.register(
            Capability.LLM,
            "my_llm",
            MyLLM(),
            plugin=self.meta.name,
            priority=self.meta.priority,
        )
        # 立即成为默认实现
        context.container.set_default(Capability.LLM, "my_llm")
```

## 示例三：注册自定义工具

```python
from chatanything import Capability, Plugin, PluginMeta, Tool, ToolContext, ToolSpec


class WeatherTool:
    spec = ToolSpec(
        name="weather",
        description="Query the weather for a city.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )

    async def invoke(self, arguments: dict, context: ToolContext) -> str:
        return f"{arguments['city']}: sunny, 24°C"


class WeatherPlugin(Plugin):
    meta = PluginMeta(name="my.weather", version="0.1.0", provides=("tool",))

    async def install(self, context) -> None:
        tool = WeatherTool()
        context.container.register(
            Capability.TOOL,
            tool.spec.name,
            tool,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )
```

工具通过 `resolve_all(Capability.TOOL)` 暴露给 ReAct 循环，并自动转换为函数调用规格。

## 加载方式

### 方式 A：通过 `HarnessConfig.plugin_modules`

```python
config = HarnessConfig(
    plugin_modules=["my_package.plugins:MyLLMPlugin"],
    plugin_config={
        "my.llm": {"model": "my-model"},
        "my.audit": {"verbose": True},
    },
)
```

支持三种引用格式：

- `"pkg.module:PluginClass"` —— 显式类；
- `"pkg.module:plugin"` —— 模块级插件实例；
- `"pkg.module"` —— 自动查找模块内的 `PLUGIN` 或 `plugin` 对象。

### 方式 B：entry points 自动发现

在第三方包的 `pyproject.toml` 中声明：

```toml
[project.entry-points."chatanything.plugins"]
my_llm = "my_package.plugins:MyLLMPlugin"
```

然后开启发现：

```python
HarnessConfig(discover_entry_points=True)
```

### 方式 C：运行时安装

```python
harness = Harness()
await harness.setup()
await harness.plugins.install(MyPlugin(), config={"key": "value"})
harness.container.set_default(Capability.LLM, "my_llm")
await harness.start()
```

需要在不重启进程的情况下动态装卸插件时，见[插件热插拔](plugin-hotplug.md)。
