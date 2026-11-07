# 配置说明

[← 返回 README](../README.md)

`HarnessConfig` 是所有部署可调参数的入口：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `agent_name` | `"ChatAnything"` | Agent 名称，写入提示词与事件 |
| `install_builtin_plugins` | `True` | 是否安装内置插件 |
| `plugin_modules` | `[]` | 外部插件引用（`module:Class`） |
| `plugin_config` | `{}` | 按插件名分组的配置字典 |
| `discover_entry_points` | `False` | 是否从 entry points 发现插件 |
| `entry_point_group` | `"chatanything.plugins"` | entry point 组名 |
| `plugin_directories` | `[]` | 启动时扫描的插件包目录列表 |
| `plugin_resource_monitoring` | `False` | 是否启用插件级 CPU/内存采样 |
| `defaults` | `{}` | 槽位默认实现覆盖，如 `{"llm": "openai_compatible"}` |
| `default_channel` | `"console"` | 未指定信道时的默认信道 |
| `batch_window_seconds` | `0.05` | Router 批处理聚合窗口 |
| `batch_max_triggers` | `16` | 单批最大触发器数 |
| `max_workers` | `4` | Worker 并发上限 |
| `worker_idle_timeout` | `45.0` | Worker 空闲回收时间（秒） |
| `max_steps` | `6` | ReAct 最大步数 |
| `tool_timeout_seconds` | `30.0` | 单个工具执行超时 |
| `context_token_budget` | `6000` | 上下文 token 预算 |
| `inline_token_limit` | `800` | 单条消息 Inline 上限，超出转 Artifact |
| `watchdog_interval` | `15.0` | Watchdog 巡检间隔 |
| `watchdog_stall_seconds` | `120.0` | 判定卡死的无活动时长 |
| `watchdog_enabled` | `True` | 是否启用 Watchdog |
| `auto_start_sources` | `True` | 是否自动启动事件源 |
| `speculation_enabled` | `False` | 是否启用投机执行 |
| `raise_on_hook_error` | `False` | Hook 异常是否向上抛出 |
| `log_level` | `logging.INFO` | 日志级别 |
| `log_format` | 标准格式 | 日志格式 |

插件级配置示例：

```python
plugin_config={
    "filter.rule": {"agent_name": "MyAgent", "require_mention": True},
    "context.inline_spill": {"token_budget": 8000, "inline_token_limit": 600},
    "loop.react": {"max_steps": 10},
    "source.interval": {"interval_seconds": 10.0},
}
```

Web 部署用到的配置项见 [Web 服务](web-service.md)。
