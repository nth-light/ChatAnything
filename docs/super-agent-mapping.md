# 机制来源与设计映射

[← 返回 README](../README.md)

## 来源

ChatAnything 的 Agent 运行机制并非凭空设计，而是**参考 DeepSeek Harness 团队的文章**：

> 《[Super-agent 的设计与实现](https://mp.weixin.qq.com/s/zoS7GlReWPhfCjENGxXCdg)》—— DeepSeek Harness 团队

文章描述的核心思路是：Agent 必须能持续运行、并发处理多路事件、主动响应环境，而不是把"过滤、路由、推理、工具、记忆"写死在单条主循环里。ChatAnything 将该文中的设计逐项落地为可替换的插件契约与生命周期点，对应关系如下。

## 对应关系

| 设计文档概念 | ChatAnything 实现 |
| --- | --- |
| 环境事件 / Trigger | `Trigger`、`EventSource`、`Harness.submit()` |
| 两级过滤（Fast / Slow Path） | `FilterPipeline` + `RuleFilter` + `LLMSemanticFilter` |
| Thread 订阅直通 | `ThreadSubscription` + `submit()` 快路径 |
| Router 全局共享上下文 | `RouterAgent` 的 Router 会话与历史 |
| Worker 上下文隔离与并发 | `WorkerSession` + `WorkerManager` + 信号量 |
| Inline & Spill、链式召回 | `ContextOrganizer` + `Artifact` + `recall` 工具 |
| 异步 ReAct、可启停工具 | `ReActAgentLoop` + `ToolExecutor` |
| 多信道交互 | `Channel` + `ChannelRouter` |
| 计算 / 状态 / 环境分离 | `MemoryStore` + `EnvironmentProvider` |
| Router 批处理 | `batch_window_seconds` / `batch_max_triggers` |
| 外部 Watchdog 与自愈 | `Watchdog` + Harness 后台巡检循环 |
| 投机执行 | `Speculator` 接口与生命周期点 |
| 持久化状态边界 | `MemoryStore` 协议（可替换为数据库/远程服务） |

架构层面的落地方式见[架构总览](architecture.md)，各扩展点的契约见[核心概念](core-concepts.md)。
