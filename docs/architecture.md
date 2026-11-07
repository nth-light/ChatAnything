# 架构总览

[← 返回 README](../README.md)

ChatAnything 采用**双层 Agent 架构**：Router 层负责全局连续性与批处理决策，Worker 层负责隔离的并发执行。两层共享同一套生命周期引擎与组件注册表，因此从过滤器到 Agent Loop 的任意环节都可以替换。

## 数据流

```
                    ┌──────────────────────────────────────────────┐
   External         │                 LifecycleEngine              │
   Events / IM ───▶ │  state machine + ordered lifecycle points    │
   Scheduler        └──────────────────────────────────────────────┘
                                   ▲        ▲
                                   │        │ hooks
   Trigger ──▶ Fast Filter ──▶ Router 队列 ──▶ RouterAgent ──▶ WorkerManager
                     │                            │                │
                     │                            │            WorkerSession × N
                     │                            │            (isolated context)
                     │                            ▼                │
                     │                     Router 会话/历史         │ reports
                     │                            ▼                ▼
                     └──── thread subscription ──▶ ChannelRouter ◀── Router
```

关键路径：

1. 外部事件（IM、Webhook、定时器、`Harness.submit()`）转换为 `Trigger`；
2. `FilterPipeline` 先执行确定性的 Fast Path，明显噪声直接丢弃；
3. 未命中的触发器进入 Router 队列，按 `batch_window_seconds` 聚合；
4. 已存在 Worker 的线程走**订阅直通**，跳过 Router 直接投递给对应 Worker；
5. `RouterAgent` 一次决策处理一批事件，必要时为线程创建 `WorkerSession`；
6. Worker 在独立上下文中运行 ReAct 循环，工具由 `ToolExecutor` 并发执行；
7. 结果以 `Report` 回传 Router，由 `ChannelRouter` 投递到目标信道。

## 分层职责

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 基础设施 | `models.py` `errors.py` `utils.py` | 域模型、异常体系、通用工具 |
| 生命周期 | `lifecycle.py` | 状态机、生命周期点、Hook 调度、事件历史 |
| 组件注册 | `registry.py` | 能力槽位、命名解析、默认实现、优先级 |
| 插件体系 | `plugins/` | 标准插件契约、接口协议、加载器、安装管理器 |
| 内置插件 | `plugins/builtin/` | 默认能力实现（可全部替换） |
| 运行时核心 | `core/` | Harness 装配、Router、Worker、过滤管线、工具执行、ReAct 循环 |

## 目录结构

```
chatanything/
├── __init__.py          # 公开 API 与版本
├── py.typed             # PEP 561 类型标记
├── config.py            # HarnessConfig
├── models.py            # Trigger / Session / Message / Report ...
├── lifecycle.py         # LifecycleState / LifecyclePoint / LifecycleEngine
├── registry.py          # Capability / ComponentRegistry
├── runtime.py           # Runtime / ChannelRouter
├── errors.py
├── utils.py
├── plugins/
│   ├── base.py          # Plugin / PluginMeta / PluginContext
│   ├── interfaces.py    # 全部 Protocol 契约
│   ├── loader.py        # module:Class 加载、entry point 发现
│   ├── manager.py       # 安装、依赖排序、卸载、热插拔事务
│   ├── hotplug/         # 清单、来源、沙箱、版本、依赖、状态
│   └── builtin/         # 15 个内置插件
└── core/
    ├── harness.py       # Harness：装配与事件调度
    ├── router.py        # RouterAgent：全局决策与批处理
    ├── worker.py        # WorkerSession / WorkerManager
    ├── filters.py       # FilterPipeline
    ├── tools.py         # ToolExecutor
    └── loops.py         # ReActAgentLoop 及其插件
examples/
├── quickstart.py
├── custom_plugin.py
├── super_agent_demo.py
├── hotplug_demo.py
└── plugins/hotplug_greet/   # 可热加载的示例插件包
docs/
├── architecture.md
├── core-concepts.md
├── plugin-development.md
├── plugin-hotplug.md        # 热插拔 API 文档
├── web-service.md
├── builtin-plugins.md
├── configuration.md
├── examples.md
├── super-agent-mapping.md
└── development.md
```

## 设计约束

- 核心运行时只通过 `ComponentRegistry` 解析能力，不直接依赖具体插件；
- 内置插件是"默认装配"，可整体关闭（`install_builtin_plugins=False`）后自行组装；
- 所有扩展点以 `Protocol` 表达，第三方实现无需继承框架类；
- 状态存储、事件源、信道、执行环境均可替换为生产级实现。

相关文档：[核心概念](core-concepts.md) · [插件开发规范](plugin-development.md) · [机制来源与设计映射](super-agent-mapping.md)
