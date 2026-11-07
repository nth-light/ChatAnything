# Web 服务（轻量部署）

[← 返回 README](../README.md)

除了控制台，ChatAnything 还自带一个 Web UI 与 REST API，由 `web.http` 插件基于标准库 `ThreadingHTTPServer` 提供，不需要任何第三方依赖。

## 启动

```bash
python -m chatanything.webapp --db ./chatanything.db
# 或安装后
chatanything-web --host 127.0.0.1 --port 8790 --db ./chatanything.db --workspace .
```

参数说明：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | HTTP 监听地址 |
| `--port` | `8790` | 监听端口，`0` 表示自动选择空闲端口 |
| `--db` | `./chatanything.db` | SQLite 数据库路径（父目录自动创建） |
| `--workspace` | 当前工作目录 | 工具与本地执行环境的工作区根目录 |
| `--log-level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

启动后终端会打印实际访问地址、数据库绝对路径与工作目录，按 `Ctrl+C` 停止并通过 `Harness.stop()` 正常关闭。

## 页面

浏览器打开 `http://127.0.0.1:8790/`，静态页面使用 Hash 路由：

| 路由 | 页面 |
| --- | --- |
| `#chat` | 对话与历史会话 |
| `#models` | 模型配置（新增、编辑、切换默认） |
| `#mcp` | MCP 服务器与 tools / resources / prompts 条目 |
| `#skills` | Skill 配置 |
| `#tools` | 已注册工具与在线调用 |

## REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | Harness 状态、能力、插件、存储统计与工具列表 |
| `POST` | `/api/chat` | 提交一条消息（`text`，可选 `actor` / `thread_id`），返回 `trigger_id` |
| `GET` | `/api/chat/stream` | SSE 逐字输出（`trigger_id` 必填，可选 `heartbeat` / `timeout`） |
| `GET` | `/api/sessions` | 会话列表（可选 `kind` / `status` / `limit`） |
| `GET` | `/api/sessions/{id}` | 会话详情 |
| `GET` | `/api/sessions/{id}/messages` | 会话消息（`limit` / `offset`） |
| `GET` `POST` | `/api/models` | 列出 / 新建模型配置 |
| `GET` `PUT` `DELETE` | `/api/models/{id}` | 读取 / 更新 / 删除模型配置 |
| `POST` | `/api/models/{id}/default` | 设为默认模型 |
| `GET` `POST` | `/api/mcp/servers` | 列出 / 新建 MCP 服务器 |
| `GET` `PUT` `DELETE` | `/api/mcp/servers/{id}` | 读取 / 更新 / 删除 MCP 服务器 |
| `GET` `PUT` `DELETE` | `/api/mcp/servers/{id}/entries[/{kind}/{name}]` | 查询 / 写入 / 删除 MCP 条目 |
| `GET` `POST` | `/api/skills` | 列出 / 新建 Skill |
| `GET` `PUT` `DELETE` | `/api/skills/{id}` | 读取 / 更新 / 删除 Skill |
| `GET` | `/api/tools` | 已注册工具及其参数规格 |
| `POST` | `/api/tools/{name}/invoke` | 调用工具（`arguments` 对象） |

## SSE 逐字输出

对话页面采用 ChatGPT 风格的服务端推送：`POST /api/chat` 返回 `trigger_id` 后，前端用 `EventSource` 订阅
`/api/chat/stream?trigger_id=...`，助手回复按 token 增量渲染。事件均为 `data: <json>`：

| `type` | 说明 |
| --- | --- |
| `start` | 任务开始（包含 `session_id` / `goal`） |
| `delta` | 增量文本片段，前端直接追加 |
| `reset` | 该步输出被判定为工具调用，清空已显示文本 |
| `tool_call` / `tool_result` | 工具调用与执行结果 |
| `done` | 结束事件，`content` 为最终完整答案，`stopped_reason` 为 `completed` / `max_steps` / `replied` / `ignored` |
| `error` | 失败或超时，包含 `message` |

断线后浏览器会自动重连，Hub 会重放该 `trigger_id` 的缓冲事件，因此不会丢失开头内容。
命令行验证：

```bash
curl -N "http://127.0.0.1:8790/api/chat/stream?trigger_id=<trigger_id>"
```

## 插件与配置

入口只启用三个插件，其余内置能力（Echo LLM、规则过滤、ReAct 循环、流式 Hub、控制台信道等）仍由 Harness 默认装配：

- `storage.sqlite`：在同一 SQLite 文件中提供 `memory` / `config_store` / `mcp` / `skill` 四类存储，并设为默认实现；
- `tools.aicoding`：注册 `web_search`、`file_edit`、`run_command` 三个工具；
- `web.http`：提供 Web UI 与 REST API，依赖前两个插件；
- `stream.memory`：内存流 Hub（`stream` 能力），为 SSE 提供广播、短时重放与订阅隔离。

对应的 `HarnessConfig.plugin_config` 片段如下（缺少的键使用插件默认值）：

```json
{
  "storage.sqlite": {"path": "./chatanything.db", "busy_timeout": 5.0, "journal_mode": "WAL"},
  "environment.local": {"cwd": ".", "allow_shell": true},
  "tools.aicoding": {"root": ".", "allow_outside": false},
  "web.http": {"host": "127.0.0.1", "port": 8790, "request_timeout": 30.0},
  "stream.memory": {"max_events_per_key": 2048, "max_keys": 256, "ttl_seconds": 600}
}
```

## 安全提示

- 默认只监听 `127.0.0.1`，不对局域网/公网开放；如需远程访问请显式指定 `--host`，并注意服务本身没有鉴权。
- `environment.local.allow_shell=true` 会允许 `run_command` 执行本地命令，仅应在可信工作区中使用。
- `tools.aicoding.root` 限制 `file_edit` 可访问的文件范围，`allow_outside` 默认为 `false`。
- 建议在隔离环境或专用低权限用户下运行，不要直接暴露到不可信网络。
