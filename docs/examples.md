# 使用示例

[← 返回 README](../README.md)

## 1. 默认离线运行

```bash
python examples/quickstart.py
```

演示：自定义 `TracePlugin` 订阅生命周期点；提交事件；等待空闲；打印状态与 Worker 会话。

## 2. 替换模型并驱动工具

```bash
python examples/custom_plugin.py
```

演示：`ScriptedLLMPlugin` 在运行时替换默认 LLM，`AuditPlugin` 通过 Hook 审计工具调用与报告，Worker 在 ReAct 循环中调用 `calculator` 完成任务。

## 3. Router 批处理与线程订阅直通

```bash
python examples/super_agent_demo.py
```

演示：

- 同时提交两个不同线程的事件 → Router 一次 `handle_batch` 处理 `batch_size=2`；
- 已存在 Worker 的线程后续事件绕过 Router，直接投递（`fast_path=True`）；
- 多信道报告回传与运行时观测数据。

## 4. 插件热插拔

```bash
python examples/hotplug_demo.py
```

演示：运行期从本地包目录加载 `example.hotplug_greet`，调用其注册的工具，禁用/启用验证能力撤销与恢复，重载生成新实例，最后卸载并打印完整状态快照（含沙箱策略、阶段耗时与内存增量）。

## 5. 接入真实模型

```python
from chatanything import Harness, HarnessConfig

config = HarnessConfig(
    defaults={"llm": "openai_compatible"},
    plugin_config={
        "llm.openai_compatible": {
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-...",          # 也可使用 OPENAI_API_KEY 环境变量
            "temperature": 0.2,
            "timeout": 60.0,
        }
    },
)
```

该实现仅使用标准库 HTTP 客户端，支持 `tools` 参数与工具调用返回，无需额外依赖。
