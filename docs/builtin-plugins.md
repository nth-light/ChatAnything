# 内置插件

[← 返回 README](../README.md)

默认安装 15 个插件，均可被同名槽位的新实现替换。

| 插件名 | 贡献 | 组件名 | 说明 |
| --- | --- | --- | --- |
| `memory.in_memory` | `memory` | `in_memory` | 会话、线程、触发器、订阅、Artifact 的内存存储 |
| `prompt.default` | `prompt` | `default` | 过滤器 / 路由器 / Worker 的默认提示词与 JSON 输出契约 |
| `parser.json_react` | `parser` | `json_react` | 解析 `final_answer` / `action` / `tool_calls`，纯文本回退 |
| `tools.core` | `tool` | `calculator` `current_time` `recall` | 安全算术、当前时间、引用链召回 |
| `channel.console` | `channel` | `console`（默认）、`memory` | 控制台输出；`memory` 捕获消息供测试与示例断言 |
| `llm.echo` | `llm` | `echo`（默认） | 离线确定性模型 |
| `llm.openai_compatible` | `llm` | `openai_compatible` | OpenAI 兼容接口，默认不启用 |
| `filter.rule` | `filter` | `rule` | Fast Path：空文本、事件类型、黑名单、@ 提及 |
| `filter.semantic` | `filter` | `semantic` | Slow Path：LLM 语义判断，异常时启发式回退 |
| `context.inline_spill` | `context` | `inline_spill` | Inline & Spill 上下文、预算控制、召回提示 |
| `router.llm` | `router_policy` | `llm_router` | 批量路由决策；异常时启发式回退 |
| `loop.react` | `agent_loop` | `react` | 异步 ReAct、工具循环、步数上限 |
| `watchdog.stall` | `watchdog` | `stall` | 检测长时间无活动的 RUNNING 会话 |
| `environment.local` | `environment` | `local` | 本地执行环境（默认禁用 shell） |
| `speculator.noop` | `speculator` | `noop` | 空实现，预留投机执行扩展 |

可选插件：

| 插件名 | 贡献 | 说明 |
| --- | --- | --- |
| `source.interval` | `event_source` | 定时主动触发源，默认不安装 |

此外，Web 部署路径还会用到 `storage.sqlite`、`tools.aicoding`、`web.http`、`stream.memory` 四个插件，详见 [Web 服务](web-service.md)。

替换方式见[插件开发规范](plugin-development.md)，运行时装卸见[插件热插拔](plugin-hotplug.md)。
