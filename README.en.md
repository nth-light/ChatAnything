# ChatAnything

> A lifecycle-driven Python agent harness where **everything is a plugin**.

ChatAnything is built for super-agents that keep running, execute concurrently, and react to their environment on their own. Every stage of the agent loop — event filtering, routing decisions, context organization, memory, prompts, output parsing, tool calls, channel delivery, watchdog, execution environment, speculative execution — is an **interchangeable plugin capability**. Replacing any module never requires touching the framework core.

- **License**: MIT
- **Python**: >= 3.10
- **Runtime dependencies**: standard library only (optional `pytest` / `mypy` / `ruff` for development)
- **Out of the box**: offline, deterministic, end-to-end runnable without an API key
- **中文**: [README.md](README.md)

---

## Origin & Implementation Notes

- **Design origin**: the agent mechanism is derived from an article by the **DeepSeek Harness team**:
  [《Super-agent 的设计与实现》(Designing and Building a Super-agent)](https://mp.weixin.qq.com/s/zoS7GlReWPhfCjENGxXCdg).
  Two-stage filtering, Router/Worker separation, thread subscription pass-through, Inline & Spill context, async ReAct,
  multi-channel delivery, watchdog, and speculative execution are all implemented here as plugins and lifecycle points.
  The item-by-item mapping is in [docs/super-agent-mapping.md](docs/super-agent-mapping.md) (Chinese).
- **How it was built**: **all code in this repository — framework core, plugin system, built-in plugins, examples,
  tests, and documentation — was written by AI**, in roughly **3 hours** from an empty directory to a runnable,
  testable, demo-ready project.
  Treat it as a working reference implementation that validates the design and demonstrates the plugin architecture.
  Add your own security review and load testing before production use.

---

## Why ChatAnything

Traditional agent frameworks hard-code filtering, routing, reasoning, tools, and memory into one main loop, so
every customization becomes a core patch. ChatAnything does the opposite:

1. **Lifecycle-driven**: a validated state machine emits fine-grained lifecycle points (`LifecyclePoint`).
   Plugins observe, audit, or inject side effects by subscribing to these points instead of patching internals.
2. **Everything is a plugin**: the core only resolves `Protocol` implementations from `ComponentRegistry` and never
   imports a concrete implementation. Swapping the LLM, filters, routing policy, or the reasoning loop means
   registering a component with higher priority.
3. **Two-tier agent architecture**: global continuity lives in the Router layer; execution isolation lives in the
   Worker layer. The Router keeps cross-thread context and dispatches in batches; Workers run concurrently in
   isolated sessions and report results back.

The framework boots with a working default assembly (in-memory storage, Echo LLM, rule + semantic filtering,
Inline & Spill context, a ReAct loop, built-in tools), and every part of it can be overridden by third-party plugins.

---

## Highlights

- **Lifecycle state machine**: `CREATED → INITIALIZING → READY → RUNNING ⇄ PAUSED → STOPPING → STOPPED`; illegal
  transitions are rejected and unrecoverable errors can move the harness to `FAILED`.
- **Fine-grained lifecycle points**: harness, plugin, trigger, session, context, model call, tool, report, channel,
  watchdog, and speculation stages are all observable.
- **14 capability slots**: `llm`, `filter`, `context`, `memory`, `router_policy`, `prompt`, `parser`, `agent_loop`,
  `channel`, `tool`, `watchdog`, `environment`, `speculator`, `event_source` — with named resolution, priorities,
  default replacement, and cleanup on uninstall.
- **Two-stage event filtering**: a deterministic fast path drops obvious noise; a semantic slow path runs on the
  Router side to save model calls.
- **Thread subscription pass-through**: follow-up events for active threads bypass the Router and go straight to
  their Worker.
- **Router batching + isolated concurrent Workers**: triggers arriving during inference are aggregated into one
  batch; each Worker owns its session and context, concurrency is bounded by a semaphore, and idle Workers are reclaimed.
- **Inline & Spill context**: oversized content spills to disk as an `Artifact` and is recalled through reference
  IDs and the `recall` tool.
- **Async ReAct + tool execution**: concurrent tools, per-call timeouts, and errors converted into `ToolResult`
  without killing the Worker.
- **Multi-channel delivery and an external watchdog**: console, memory, IM, or webhook channels are dispatched
  uniformly; stalled sessions are detected and best-effort recovered.
- **Hot plug-in at runtime**: load, unload, enable, disable, reload, and update plugins without restarting the
  process, wrapped in transactions that roll back on failure.
- **Typed and dependency-free**: public APIs are fully annotated (`py.typed`) and the core uses only the standard library.

---

## Quick Start

### 1. Install

```bash
cd chat_anything
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

pip install -e .
```

### 2. Run an example

```bash
python examples/quickstart.py
```

`quickstart.py` prints the lifecycle trace, harness state, installed plugins, and worker sessions.
The default assembly is fully offline and needs no API key.

### 3. Minimal code

```python
import asyncio
from chatanything import Harness, HarnessConfig


async def main() -> None:
    config = HarnessConfig(agent_name="ChatAnything", log_level=30)
    async with Harness(config) as harness:
        await harness.submit(
            "Please confirm the plan is feasible and list the main risks.",
            actor="alice",
            thread_id="thread-demo",
        )
        await harness.wait_until_idle(timeout=15)
        print("state:", harness.state.value)
        print("plugins:", ", ".join(harness.plugins.installed_names()))


if __name__ == "__main__":
    asyncio.run(main())
```

`async with` runs `setup()` then `start()` and calls `stop()` on exit. Manual control works too:

```python
harness = Harness(HarnessConfig())
await harness.setup()
await harness.start()
await harness.submit("Hello")
await harness.wait_until_idle()
await harness.stop()
```

---

## Plugin in 30 Seconds

```python
from chatanything import Capability, Plugin, PluginMeta


class MyLLM:
    name = "my_llm"

    async def complete(self, messages, *, tools=(), session=None):
        from chatanything import LLMResponse
        return LLMResponse(content="(my model response)")


class MyLLMPlugin(Plugin):
    meta = PluginMeta(name="my.llm", version="0.1.0", provides=("llm",), priority=10)

    async def install(self, context) -> None:
        context.container.register(
            Capability.LLM,
            "my_llm",
            MyLLM(),
            plugin=self.meta.name,
            priority=self.meta.priority,
        )
        context.container.set_default(Capability.LLM, "my_llm")
```

Load it with `HarnessConfig(plugin_modules=["my_package.plugins:MyLLMPlugin"])`, publish it through the
`chatanything.plugins` entry point group, or install it at runtime via `harness.plugins.install(...)`.

---

## Web UI and REST API

A zero-dependency Web UI plus REST API ships with the `web.http` plugin (standard-library `ThreadingHTTPServer`):

```bash
python -m chatanything.webapp --db ./chatanything.db
# or, after installation
chatanything-web --host 127.0.0.1 --port 8790 --db ./chatanything.db --workspace .
```

Open `http://127.0.0.1:8790/` for chat with streaming output, model configuration, MCP servers, skills, and tools.
Endpoints include `/api/chat`, `/api/chat/stream` (SSE), `/api/sessions`, `/api/models`, `/api/mcp/servers`,
`/api/skills`, and `/api/tools`. Details: [docs/web-service.md](docs/web-service.md) (Chinese).

> Security: the server binds to `127.0.0.1` and has no authentication. Do not expose it to untrusted networks.

---

## Documentation

The README stays at overview level; details live in `docs/` (currently written in Chinese):

| Document | Contents |
| --- | --- |
| [架构总览 / Architecture](docs/architecture.md) | Data flow, layers, directory layout, design constraints |
| [核心概念 / Core concepts](docs/core-concepts.md) | State machine, lifecycle points, capability slots, plugin model |
| [插件开发规范 / Plugin guide](docs/plugin-development.md) | Rules, three worked examples, three loading strategies |
| [插件热插拔 / Hot plug-in](docs/plugin-hotplug.md) | Runtime load/unload, sandboxing, dependency resolution, monitoring, API reference |
| [Web 服务 / Web service](docs/web-service.md) | Web UI, REST API, SSE streaming, security notes |
| [内置插件 / Built-in plugins](docs/builtin-plugins.md) | The 15 default plugins and optional ones |
| [配置说明 / Configuration](docs/configuration.md) | Every `HarnessConfig` field and per-plugin config |
| [使用示例 / Examples](docs/examples.md) | Four runnable demos and real-model setup |
| [机制来源与设计映射 / Origin & mapping](docs/super-agent-mapping.md) | Mapping to the DeepSeek Harness article |
| [开发与验证 / Development](docs/development.md) | Lint, type-check, tests, and directory conventions |

Example commands:

```bash
python examples/quickstart.py        # lifecycle trace, offline
python examples/custom_plugin.py     # replace the LLM, audit tool calls
python examples/super_agent_demo.py  # Router batching and thread pass-through
python examples/hotplug_demo.py      # load/disable/reload/unload at runtime
python -m pytest -q
```

---

## License

Released under the [MIT License](LICENSE).

Copyright (c) 2026 ChatAnything Contributors
