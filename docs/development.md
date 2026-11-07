# 开发与验证

[← 返回 README](../README.md)

```bash
# 开发依赖
pip install -e ".[dev]"

# 静态检查
ruff check chatanything examples
mypy chatanything

# 编译检查
python -m compileall -q chatanything examples

# 端到端示例
python examples/quickstart.py
python examples/custom_plugin.py
python examples/super_agent_demo.py
python examples/hotplug_demo.py

# 单元测试
python -m pytest -q
python -m pytest tests/test_plugin_hotplug.py -q
```

## 设计约束

- 核心运行时只通过 `ComponentRegistry` 解析能力，不直接依赖具体插件；
- 内置插件是"默认装配"，可整体关闭（`install_builtin_plugins=False`）后自行组装；
- 所有扩展点以 `Protocol` 表达，第三方实现无需继承框架类；
- 状态存储、事件源、信道、执行环境均可替换为生产级实现。

## 目录约定

- 框架代码位于 `chatanything/`，示例位于 `examples/`，测试位于 `tests/`；
- 文档位于 `docs/`，每个专题独立成篇，README 只保留概览与导航；
- 新增插件时，建议同时在 `examples/` 下提供最小可运行示例。
