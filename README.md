# ChatAnything

> 一个以**生命周期**为核心驱动、**一切皆为插件**的 Python Agent Harness 框架。

ChatAnything 用于构建可持续运行、可并发执行、可主动响应环境的超级 Agent（Super-agent）。框架把 Agent 运行中的每一个环节——事件过滤、路由决策、上下文组织、记忆存储、提示词、输出解析、工具调用、信道投递、看门狗、执行环境、投机执行——全部抽象为**可替换的插件能力**。扩展或替换任意模块都不需要修改框架核心源码。

- **许可证**：MIT
- **Python**：>= 3.10
- **运行时依赖**：仅标准库（可选 `pytest` / `mypy` / `ruff` 用于开发）
- **默认体验**：离线、确定性，无需 API Key 即可端到端运行
- **English**：[README.en.md](README.en.md)

---

## 来源与实现声明

- **机制来源**：本项目的 Agent 运行机制参考 **DeepSeek Harness 团队**的文章
  [《Super-agent 的设计与实现》](https://mp.weixin.qq.com/s/zoS7GlReWPhfCjENGxXCdg)。
  文章中的两级过滤、Router / Worker 分层、线程订阅直通、Inline & Spill 上下文、异步 ReAct、多信道、Watchdog 与投机执行等设计，
  均在 ChatAnything 中以插件与生命周期点的形式落地。逐项映射见[机制来源与设计映射](docs/super-agent-mapping.md)。
- **实现方式**：本项目的全部代码（框架核心、插件体系、内置插件、示例、测试与文档）**均由 AI 实现**，
  从零搭建到可运行、可测试、可演示的整体耗时约 **3 小时**。
  因此项目更接近一套"可运行的参考实现"，用于验证设计思路与展示插件化架构；在生产环境使用前，请自行补充安全审计与压测。

---

## 项目简介

传统 Agent 框架通常把"过滤、路由、推理、工具、记忆"写死在主循环里，导致任何定制都需要侵入核心。ChatAnything 采用相反的设计：

1. **生命周期驱动**：框架维护一个经过校验的状态机，并在运行过程中发出细粒度生命周期点（`LifecyclePoint`）。插件通过订阅这些点来观察、审计、注入副作用，而不是打补丁。
2. **一切皆为插件**：核心只依赖 `ComponentRegistry` 中解析出的接口实现（`Protocol`），从不直接 import 具体实现。替换 LLM、过滤器、路由策略、推理循环等，只需注册优先级更高的组件。
3. **双层 Agent 架构**：参考 Super-agent 设计文档，将**全局连续性**（Router 层）与**执行隔离性**（Worker 层）分离；Router 维护跨线程上下文并成批调度，Worker 在独立会话中并发执行并通过报告回传。

框架启动即自带一套可运行的默认插件组合（内存存储、Echo LLM、规则 + 语义过滤、Inline & Spill 上下文、ReAct 循环、计算器等），同时保持所有部件可被第三方插件覆盖。

---

## 核心特性

- **生命周期状态机**：`CREATED → INITIALIZING → READY → RUNNING ⇄ PAUSED → STOPPING → STOPPED`，非法转换会被拒绝；失败可进入 `FAILED`。
- **细粒度生命周期点**：覆盖 Harness、插件、事件、会话、上下文、模型调用、工具、报告、信道、看门狗、投机执行等阶段，全部可订阅。
- **一切皆为插件**：14 类能力槽位，插件可贡献一个或多个实现；组件支持命名解析、优先级、默认替换与卸载清理。
- **事件驱动的两级过滤**：确定性 Fast Path 先过滤明显噪声，再在 Router 侧执行语义 Slow Path，降低模型调用成本。
- **线程订阅直通**：活跃线程的后续事件绕过 Router，直接投递给对应 Worker，兼顾连续性与低延迟。
- **Router 批处理 + Worker 隔离并发**：推理期间到达的触发器按窗口聚合；每个 Worker 拥有独立 Session 与上下文，受信号量限制并发度，空闲超时自动回收。
- **Inline & Spill 上下文**：超长内容自动落盘为 `Artifact`，通过唯一引用 ID 与 `recall` 工具链式召回。
- **异步 ReAct + 工具执行**：工具并发执行、超时保护、异常转 `ToolResult`，不中断 Worker。
- **多信道 + 外部 Watchdog**：控制台、内存、IM、Webhook 等信道统一投递；周期巡检卡死会话并尽力自愈。
- **插件热插拔**：运行期加载、卸载、启用、禁用、重载与更新插件，事务化执行并支持失败回滚。
- **完整类型注解 + 零运行时依赖**：公开 API 均带类型注解（`py.typed`），核心仅使用标准库，便于嵌入现有服务。

---

## 快速开始

### 1. 安装

```bash
cd chat_anything
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

pip install -e .
```

### 2. 运行示例

```bash
python examples/quickstart.py
```

`quickstart.py` 会打印生命周期轨迹、Harness 状态、已安装插件与 Worker 会话列表。默认组合完全离线，不需要任何 API Key。

### 3. 最小代码

```python
import asyncio
from chatanything import Harness, HarnessConfig


async def main() -> None:
    config = HarnessConfig(agent_name="ChatAnything", log_level=30)
    async with Harness(config) as harness:
        await harness.submit(
            "请帮我确认方案是否可行，并列出主要风险。",
            actor="alice",
            thread_id="thread-demo",
        )
        await harness.wait_until_idle(timeout=15)
        print("state:", harness.state.value)
        print("plugins:", ", ".join(harness.plugins.installed_names()))


if __name__ == "__main__":
    asyncio.run(main())
```

`async with` 会依次执行 `setup()`、`start()`，退出时执行 `stop()`。也可以手动控制：

```python
harness = Harness(HarnessConfig())
await harness.setup()
await harness.start()
await harness.submit("你好")
await harness.wait_until_idle()
await harness.stop()
```

---

## 文档导航

README 只保留概览，细节拆到 `docs/` 下，按需阅读：

| 文档 | 内容 |
| --- | --- |
| [架构总览](docs/architecture.md) | 数据流、分层职责、目录结构与设计约束 |
| [核心概念](docs/core-concepts.md) | 生命周期状态机与生命周期点、能力槽位、插件模型 |
| [插件开发规范](docs/plugin-development.md) | 开发约束、审计/替换 LLM/自定义工具三个示例、三种加载方式 |
| [插件热插拔](docs/plugin-hotplug.md) | 运行时装卸、沙箱与来源校验、依赖解析、监控与 API 参考 |
| [Web 服务](docs/web-service.md) | Web UI、REST API、SSE 逐字输出与安全提示 |
| [内置插件](docs/builtin-plugins.md) | 15 个默认插件与可选插件清单 |
| [配置说明](docs/configuration.md) | `HarnessConfig` 全部字段与插件级配置 |
| [使用示例](docs/examples.md) | 四个可运行示例与接入真实模型 |
| [机制来源与设计映射](docs/super-agent-mapping.md) | 与 DeepSeek Harness 团队文章逐项对应关系 |
| [开发与验证](docs/development.md) | 静态检查、测试、示例验证与目录约定 |

---

## 许可证

本项目采用 [MIT License](LICENSE)。

Copyright (c) 2026 ChatAnything Contributors
