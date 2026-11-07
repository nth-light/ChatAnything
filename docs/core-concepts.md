# 核心概念

[← 返回 README](../README.md)

## 生命周期

### 状态机

```
CREATED ──▶ INITIALIZING ──▶ READY ──▶ RUNNING ⇄ PAUSED
                                     │        │
                                     ▼        ▼
                                   STOPPING ──▶ STOPPED

任意运行阶段发生不可恢复错误 ──▶ FAILED ──▶ STOPPING / STOPPED
```

- `setup()`：安装插件 → 应用默认覆盖 → 构建 Runtime、过滤管线、Router、Worker 管理器。
- `start()`：启动事件源、Router 循环、Watchdog 循环。
- `pause()` / `resume()`：暂停/恢复 Router 消费（通过内部闸门）。
- `stop()`：停止事件源与后台循环，关闭 Worker 与 Router 会话，逆序卸载插件。

非法状态转换会抛出 `InvalidTransitionError`。

### 生命周期点

| 分组 | 生命周期点 | 说明 |
| --- | --- | --- |
| Harness | `harness_setup` `harness_ready` `harness_started` `harness_tick` `harness_paused` `harness_resumed` `harness_stopping` `harness_stopped` `harness_error` | 装配、启动、每批调度 tick、暂停恢复、停止与错误 |
| 插件 | `plugin_installed` `plugin_uninstalled` | 插件安装与卸载 |
| 事件 | `trigger_ingested` `trigger_filtered` `trigger_routed` `trigger_ignored` | 事件摄入、过滤判定、路由/直通、忽略 |
| 会话 | `session_created` `session_started` `session_reported` `session_closed` `session_failed` | Worker/Router 会话生命周期 |
| 上下文/模型 | `context_building` `context_built` `before_model_call` `after_model_call` `output_parsed` | 上下文组装与模型调用 |
| 工具 | `tool_call_started` `tool_call_finished` | 工具执行前后 |
| 交互 | `report_received` `channel_delivered` | 报告回传与信道投递 |
| Watchdog | `watchdog_checked` `watchdog_recovered` | 巡检与自愈 |
| 投机执行 | `speculation_prepared` `speculation_adopted` `speculation_discarded` | 预执行准备、采纳与丢弃 |

订阅示例：

```python
from chatanything import LifecyclePoint

async def on_report(event):
    report = event.payload["report"]
    print(f"worker {report.session_id} finished in {report.steps} steps")

harness.lifecycle.subscribe(
    LifecyclePoint.SESSION_REPORTED,
    on_report,
    owner="my.audit",
    priority=100,   # 数值越小越先执行
)
```

Hook 规则：

- 支持同步函数或 `async` 函数；
- 默认按 `(priority, 订阅顺序)` 串行执行；
- 单个 Hook 异常会被记录到 `lifecycle.hook_errors` 并继续执行后续 Hook（`raise_on_hook_error=True` 可改为抛出）；
- `once=True` 的 Hook 执行一次后自动移除。

## 能力槽位

`Capability` 定义框架的全部扩展点。插件把实现注册到槽位，运行时按默认名或显式名解析。

| 槽位 | 契约 | 典型实现 |
| --- | --- | --- |
| `llm` | `LLMProvider` | Echo、OpenAI 兼容、本地模型 |
| `filter` | `EventFilter` | 规则过滤、语义过滤 |
| `context` | `ContextOrganizer` | Inline & Spill 上下文组织 |
| `memory` | `MemoryStore` | 内存、SQLite、Postgres、远程状态服务 |
| `router_policy` | `RouterPolicy` | LLM 路由决策、启发式策略 |
| `prompt` | `PromptProvider` | 过滤器/路由器/Worker 提示词模板 |
| `parser` | `OutputParser` | JSON / 函数调用 / 自定义格式解析 |
| `agent_loop` | `AgentLoop` | 异步 ReAct、Plan-and-Execute |
| `channel` | `Channel` | 控制台、内存、IM、邮件、Webhook |
| `tool` | `Tool` | 计算器、时间、召回、自定义工具 |
| `watchdog` | `Watchdog` | 卡死检测与自愈 |
| `environment` | `EnvironmentProvider` | 本地/容器/远程执行环境 |
| `speculator` | `Speculator` | 投机执行策略 |
| `event_source` | `EventSource` | 定时器、IM Bot、Webhook 监听 |

注册与替换：

```python
from chatanything import Capability

# 注册一个实现，并设为槽位默认
container.register(Capability.LLM, "my_llm", MyLLM(), plugin="my.llm", priority=10)

# 或在已注册的多个实现中切换默认
container.set_default(Capability.LLM, "my_llm")

# 读取
llm = container.resolve(Capability.LLM)
tools = container.resolve_all(Capability.TOOL)
```

同名组件重复注册且 `replace=False` 时抛出 `DuplicateComponentError`；插件卸载会自动移除其贡献的所有组件。

## 插件模型

```python
from chatanything import Plugin, PluginContext, PluginMeta

class MyPlugin(Plugin):
    meta = PluginMeta(
        name="my.plugin",            # 全局唯一
        version="0.1.0",
        description="...",
        requires=("memory.in_memory",),   # 依赖的插件名
        provides=("llm",),                # 声明贡献的槽位
        priority=10,                      # 安装排序；数值越小越早
    )

    async def install(self, context: PluginContext) -> None:
        ...

    async def uninstall(self, context: PluginContext) -> None:
        ...  # 可选：释放外部资源；注册表清理自动完成

    def hooks(self):
        return {...}  # 可选：由插件管理器自动订阅并在卸载时移除
```

`PluginContext` 提供：

| 属性/方法 | 说明 |
| --- | --- |
| `context.name` | 插件名 |
| `context.config` | 该插件的配置字典 |
| `context.setting(key, default)` | 读取单个配置项 |
| `context.container` | 组件注册表 |
| `context.lifecycle` | 生命周期引擎 |
| `context.logger` | 插件专属 Logger |

插件管理器（`PluginManager`）：

- `install(plugin)`：安装单个插件，校验依赖，自动订阅 `hooks()`；
- `install_all(plugins)`：按依赖拓扑与 `priority` 批量安装；
- `load_references([...])`：按 `module:Class` 引用加载；
- `discover(group)`：从 entry points 自动发现；
- `uninstall(name)` / `shutdown()`：解除 Hook、调用 `uninstall`、清理组件，`shutdown` 逆序执行。

相关文档：[插件开发规范](plugin-development.md) · [插件热插拔](plugin-hotplug.md)
