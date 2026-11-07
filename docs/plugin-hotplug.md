# 插件热插拔机制

[← 返回 README](../README.md)

ChatAnything 的插件热插拔机制允许在 Harness 运行过程中动态完成插件的**加载、卸载、启用、禁用、重载与更新**，全部操作无需重启主程序。所有变更以事务方式执行：任一阶段失败都会自动回滚注册表与生命周期订阅，主程序继续正常运行。

本文覆盖插件开发、注册、管理与监控的全流程。

---

## 目录

- [设计目标与约束](#设计目标与约束)
- [架构与模块](#架构与模块)
- [插件生命周期接口](#插件生命周期接口)
- [插件包与清单](#插件包与清单)
- [插件来源](#插件来源)
- [沙箱隔离](#沙箱隔离)
- [版本与依赖解析](#版本与依赖解析)
- [管理器 API](#管理器-api)
- [原子性与回滚](#原子性与回滚)
- [状态监控](#状态监控)
- [Harness 集成与配置](#harness-集成与配置)
- [完整示例](#完整示例)
- [单元测试](#单元测试)
- [安全说明](#安全说明)

---

## 设计目标与约束

| 目标 | 实现方式 |
| --- | --- |
| 四大生命周期钩子 | `on_initialize` / `on_start` / `on_stop` / `on_destroy` |
| 环境隔离 | 独立模块命名空间 + 导入守卫 + 注册表/生命周期门面 + 超时 |
| 本地与远程来源 | `DirectorySource` / `ArchiveSource` / `RemoteSource` |
| 实时监控 | `PluginStatus` / `PluginMetrics` / `process_usage()` |
| 依赖与冲突检测 | 版本约束校验 + Kahn 拓扑排序 + 环检测 |
| 原子回滚 | 注册表快照 + 生命周期订阅快照 + `asyncio.Lock` 串行化 |

约束：

- 运行时仅依赖标准库（`asyncio`、`zipfile`、`urllib`、`tracemalloc`）。
- 沙箱为**协作式隔离**，用于约束行为良好的插件、防止命名与资源冲突；它**不是对抗恶意代码的安全边界**。不可信插件应运行在外部进程或容器中。
- 所有热插拔操作由同一把 `asyncio.Lock` 串行化，保证并发调用不会破坏内部一致性。

---

## 架构与模块

```
chatanything/plugins/
├── base.py                 # Plugin / PluginMeta / PluginContext
├── loader.py               # module:Class 引用加载、entry point 发现
├── manager.py              # PluginManager：热插拔事务、依赖、监控
└── hotplug/
    ├── manifest.py         # plugin.json 解析、字段校验、宿主兼容性
    ├── versioning.py       # Version / Specifier / Requirement（无第三方依赖）
    ├── dependencies.py     # 依赖图排序、版本冲突与循环检测
    ├── sources.py          # 目录 / 归档 / 远程来源与安全检查
    ├── sandbox.py          # 命名空间、导入守卫、门面、策略
    ├── state.py            # PluginState / PluginMetrics / PluginStatus
    └── resources.py        # CPU / 内存采样
```

数据流：

```
source(stage) → manifest(validate) → sandbox(load) → candidate
      → dependency order → install → hooks → on_initialize → on_start
      → registry commit（失败则整体回滚）
```

---

## 插件生命周期接口

插件继承 `Plugin`，只需覆盖需要的方法。全部方法均可省略（默认空实现）。

```python
from chatanything import Plugin, PluginContext, PluginMeta


class DemoPlugin(Plugin):
    meta = PluginMeta(name="demo.plugin", version="1.0.0", provides=("tool",))

    async def install(self, context: PluginContext) -> None:
        """注册能力。此时尚未启动，失败会被回滚。"""

    async def on_initialize(self, context: PluginContext) -> None:
        """初始化阶段：准备状态、打开连接、校验配置。"""

    async def on_start(self, context: PluginContext) -> None:
        """启动阶段：启动后台任务、预热缓存。"""

    async def on_stop(self, context: PluginContext) -> None:
        """停止阶段：静默工作，但保留已分配的资源。"""

    async def on_destroy(self, context: PluginContext) -> None:
        """销毁阶段：释放生命周期中获取的全部资源。"""

    async def uninstall(self, context: PluginContext) -> None:
        """卸载：释放外部资源。注册表清理由管理器自动完成。"""
```

### 阶段顺序

| 操作 | 执行顺序 |
| --- | --- |
| 加载 / 安装 | `install` → 订阅 `hooks()` → `on_initialize` → `on_start` |
| 停止插件 | `on_stop` |
| 卸载 / 禁用后销毁 | `on_stop`（若在运行）→ 撤销能力 → `on_destroy` → `uninstall` → 释放沙箱与暂存目录 |
| 重载 / 更新 | 旧实例 `on_stop` → 移除旧能力 → 新实例完整安装流程 → 旧实例 `on_destroy` / `uninstall` |

> `on_*` 命名而非裸 `start`/`stop`，是为了让同时实现 `EventSource`（自带 `start(emit)` / `stop()`）的插件不产生签名冲突。

### 生命周期点

热插拔过程中会发出以下事件（`LifecyclePoint`）：

| 生命周期点 | 触发时机 |
| --- | --- |
| `PLUGIN_VALIDATED` | 清单与包校验通过、开始安装前 |
| `PLUGIN_INSTALLED` | 安装完成（能力已注册） |
| `PLUGIN_INITIALIZED` | `on_initialize` 完成 |
| `PLUGIN_STARTED` | `on_start` 完成，进入 RUNNING |
| `PLUGIN_STOPPED` | `on_stop` 完成 |
| `PLUGIN_DISABLED` / `PLUGIN_ENABLED` | 禁用 / 启用完成 |
| `PLUGIN_RELOADED` | 重载 / 更新完成（含前后版本） |
| `PLUGIN_DESTROYED` | 销毁完成 |
| `PLUGIN_UNINSTALLED` | 卸载流程结束 |
| `PLUGIN_ROLLED_BACK` | 事务失败并已回滚 |

### PluginContext

| 成员 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `str` | 插件名 |
| `config` | `Mapping[str, Any]` | 只读配置（`MappingProxyType`） |
| `setting(key, default)` | `Any` | 读取单个配置项 |
| `container` | `SandboxedRegistry` | 受控注册表门面，只能操作本插件拥有的组件 |
| `lifecycle` | `SandboxedLifecycle` | 受控生命周期门面，订阅自动归属 `plugin:<name>` |
| `logger` | `Logger` | `chatanything.plugin.<name>` |
| `manifest` | `PluginManifest \| None` | 解析后的清单 |
| `sandbox` | `dict \| None` | 沙箱命名空间与策略描述 |

门面的写操作在插件被撤销（禁用/卸载/重载）后会变为 no-op，因此残留引用不会污染注册表。

---

## 插件包与清单

一个插件包是一个目录，包含清单文件与入口代码：

```
my_plugin/
├── plugin.json      # 或 manifest.json
└── plugin.py
```

### 清单字段

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | string | 是 | — | 全局唯一名，字符集 `[A-Za-z0-9_.\-]` |
| `version` | string | 是 | — | 数字点分版本，最多 4 段 |
| `entry` | string | 否 | — | 入口，格式 `模块路径[:属性名]`，如 `plugin.py:DemoPlugin` |
| `description` | string | 否 | `""` | 描述 |
| `author` | string | 否 | `""` | 作者 |
| `requires` | array \| object | 否 | `[]` | 依赖，见[版本与依赖解析](#版本与依赖解析) |
| `provides` | array | 否 | `[]` | 声明的能力槽位 |
| `priority` | int | 否 | `10` | 安装排序，数值越小越早 |
| `api` | string | 否 | `">=1.0,<2.0"` | 所需的宿主插件 ABI 版本约束 |
| `python` | string | 否 | `">=3.10"` | 所需的 Python 版本约束 |
| `sandbox` | object | 否 | `{}` | 沙箱策略，见[沙箱隔离](#沙箱隔离) |
| `metadata` | object | 否 | `{}` | 自定义元数据，原样透传 |

`entry` 省略时，加载器会尝试模块内的 `PLUGIN` 或 `plugin` 对象。`entry` 不允许绝对路径或 `..` 越界。

### 示例

```json
{
  "name": "example.hotplug_greet",
  "version": "1.0.0",
  "entry": "plugin.py:GreetPlugin",
  "description": "Hot-plug demo: registers a greeting tool at runtime.",
  "provides": ["tool"],
  "requires": ["memory.in_memory>=1.0.0"],
  "priority": 30,
  "api": ">=1.0,<2.0",
  "python": ">=3.10",
  "sandbox": {
    "allow_network": false,
    "allow_subprocess": false,
    "deny_imports": ["my_private_module"],
    "max_hook_seconds": 10
  }
}
```

### 合法性校验

加载时依次校验：

1. 清单存在且为合法 JSON；
2. `name` / `version` / `entry` 格式合法且无路径越界；
3. `api` 约束满足宿主 `PLUGIN_API_VERSION`（当前 `1.0`）；
4. `python` 约束满足当前解释器版本；
5. 入口模块可导入，且解析结果确为 `Plugin` 实例或可实例化；
6. 类声明的 `meta.name` 与 `meta.version` 与清单一致；
7. 依赖与版本约束可满足（见下一节）。

任一失败抛出 `PluginValidationError` / `PluginVersionConflictError` / `PluginLoadError`，且不会留下任何注册表或订阅痕迹。

---

## 插件来源

`PluginManager.load()` 与 `update()` 接受以下任意一种来源：

| 来源 | 形式 | 说明 |
| --- | --- | --- |
| 本地目录 | `"path/to/plugin"` 或 `Path` | 目录内有清单；也支持只有唯一子包 |
| 本地归档 | `"plugin.zip"` 或 `Path` | zip 包内包含清单 |
| 远程地址 | `"https://host/plugin.zip"` | 支持 `http` / `https` / `file` |
| 模块引用 | `"pkg.module:PluginClass"` | 由宿主进程直接导入（不启用沙箱） |
| 显式来源对象 | `DirectorySource` / `ArchiveSource` / `RemoteSource` | 可携带校验和等参数 |

自动推断规则（`source_from`）：

- 含 `://` 且协议为 `http`/`https`/`file` → `RemoteSource`；
- 路径为目录 → `DirectorySource`；
- 路径以 `.zip` 结尾且存在 → `ArchiveSource`；
- 其余视为模块引用。

直接构造来源对象可以显式控制安全参数：

```python
from chatanything import ArchiveSource, DirectorySource, RemoteSource

DirectorySource("plugins/my_plugin")
ArchiveSource("dist/my_plugin.zip", checksum="sha256:<hex>")
RemoteSource(
    "https://example.com/plugins/my_plugin.zip",
    checksum="sha256:<hex>",
    timeout=20.0,
    max_bytes=50 * 1024 * 1024,
)
```

校验和规则：

- 可写裸 64 位十六进制，或 `sha256:<hex>`；
- 大小写不敏感；
- 对下载/读取的**归档字节**计算 SHA-256，与期望值比对，不一致抛 `PluginSourceError`。

安全检查：

- 远程下载受 `Content-Length` 与累计字节双重限制（默认 50 MB）；
- 解压拒绝绝对路径、`..` 穿越与符号链接条目（zip-slip / symlink 防护）；
- 下载超时默认 20 秒；
- 远程与归档包在临时目录暂存，安装完成或失败后自动清理。

---

## 沙箱隔离

每个通过包来源加载的插件都会获得一个 `PluginSandbox`，提供四层隔离：

1. **独立模块命名空间**：`_ca_plugin_<safe_name>_<8位随机>`，同名的两次加载互不覆盖，也不与宿主模块冲突；子模块由自定义 `MetaPathFinder` 只在包根内解析。
2. **导入守卫**：替换该命名空间的 `__import__`，按策略拦截模块；被拦截时抛出 `ImportError`。
3. **注册表门面**：`SandboxedRegistry` 只允许注册/删除/设默认**本插件拥有**的组件，跨插件写操作抛 `PluginSandboxError`；插件被撤销后所有写操作变为 no-op。
4. **生命周期门面 + 超时**：`SandboxedLifecycle` 把订阅归属强制为 `plugin:<name>`，卸载时统一移除；每个阶段与 Hook 都受 `max_hook_seconds` 限制（异步超时或同步耗时校验）。

另一个关键点：入口模块**每次直接从源码编译**，不读取 `.pyc`。热更新常发生在同一秒内、文件大小不变的场景，复用陈旧字节码会导致“更新后仍是旧逻辑”的隐蔽问题。

### SandboxPolicy

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `allow_network` | `True` | 为 `False` 时拒绝网络相关模块（`socket`、`ssl`、`http`、`urllib.request`、`requests` 等） |
| `allow_subprocess` | `False` | 为 `False` 时拒绝 `subprocess`、`multiprocessing`、`ctypes`、`pty`、`resource`、`cffi` |
| `deny_imports` | `frozenset()` | 额外禁止的模块名（支持前缀匹配） |
| `max_hook_seconds` | `30.0` | 单个生命周期阶段 / Hook 的执行上限（秒） |

清单中的 `sandbox` 字段会与该插件的策略合并；宿主也可通过 `PluginManager(..., sandbox_policy=...)` 设置全局基线策略。

```python
from chatanything import SandboxPolicy

manager = PluginManager(
    container,
    lifecycle,
    sandbox_policy=SandboxPolicy(
        allow_network=False,
        allow_subprocess=False,
        deny_imports=frozenset({"pickle", "marshal"}),
        max_hook_seconds=5.0,
    ),
)
```

查看某插件的有效策略与命名空间：

```python
status = harness.plugins.status_of("example.hotplug_greet")
print(status.sandbox)
# {"namespace": "_ca_plugin_example_hotplug_greet_1a2b3c4d",
#  "root": ".../hotplug_greet",
#  "policy": {"allow_network": false, ..., "effective_denials": [...]}}
```

隔离边界说明：

- 本沙箱约束的是**导入与注册表访问**，不阻止文件系统访问、反射或已导入模块的能力；
- 完全隔离与崩溃隔离需要外部进程/容器，本机制不提供；
- 通过 `module:Class` 引用加载的插件运行在宿主模块空间，不获得独立命名空间。

---

## 版本与依赖解析

### 版本语法

- 数字点分，最多 4 段：`1`、`1.2`、`1.2.3`、`1.2.3.4`；
- 比较采用**零填充**语义：`1.2 == 1.2.0`；
- 不支持预发布标识、epoch、本地版本串。

### 约束运算符

| 运算符 | 语义 |
| --- | --- |
| `==1.2` | 精确相等（零填充后） |
| `!=1.2` | 不相等 |
| `>=1.2` / `>1.2` | 大于等于 / 大于 |
| `<=1.2` / `<1.2` | 小于等于 / 小于 |
| `~=1.2` | 兼容版本：`>=1.2,<2.0` |
| `~=1.2.3` | 兼容版本：`>=1.2.3,<1.3.0` |
| 裸 `1.2` | 等价于 `==1.2` |
| 多约束 | 逗号分隔，全部满足，如 `>=1.0,<2.0` |

### 依赖声明

`requires` 支持两种写法：

```json
["storage.sqlite>=1.0,<2.0", "memory.in_memory"]
```

```json
{
  "storage.sqlite": ">=1.0,<2.0",
  "memory.in_memory": null
}
```

### 解析与校验

1. **批量排序**：`load_many` / `install_all` / `load_directory` 会构造依赖图，用 Kahn 拓扑排序确定安装顺序；同层按 `priority` 升序、再按输入索引排序。
2. **缺失依赖**：包内未提供、且已安装插件中也不存在 → `PluginDependencyError`。
3. **版本冲突**：已安装版本不满足约束 → `PluginVersionConflictError`。
4. **循环依赖**：图中存在环 → `PluginDependencyCycleError`。
5. **单插件加载**：`load()` 校验其依赖在**已安装集合**中可满足；依赖必须已存在（不会自动下载）。

约束失败发生在任何副作用之前，候选包会被释放，注册表不受影响。

---

## 管理器 API

`PluginManager` 通过 `harness.plugins` 访问。所有变更方法内部持锁，可安全并发调用。

### 安装与加载

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `install(plugin, *, config=None, autostart=True)` | `InstalledPlugin` | 安装已实例化插件（无模块隔离） |
| `install_all(plugins, *, configs=None, autostart=True)` | `list[InstalledPlugin]` | 原子批量安装，按依赖与优先级排序 |
| `load_references(references, *, configs=None, autostart=True)` | `list[InstalledPlugin]` | 按 `module:Class` 引用加载 |
| `discover(group="chatanything.plugins", *, configs=None, autostart=True)` | `list[InstalledPlugin]` | 从 entry points 发现并安装 |
| `load(source, *, config=None, autostart=True)` | `InstalledPlugin` | 从目录/归档/远程/模块引用加载单个插件 |
| `load_many(sources, *, configs=None, autostart=True)` | `list[InstalledPlugin]` | 原子批量加载（含依赖排序） |
| `load_directory(path, *, configs=None, autostart=True)` | `list[InstalledPlugin]` | 加载目录本身或其中所有包/归档 |

### 卸载、启用与更新

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `unload(name)` | `None` | 卸载；插件不存在时抛 `PluginError` |
| `uninstall(name)` | `None` | 幂等卸载（不存在则忽略），用于关闭流程 |
| `disable(name)` | `InstalledPlugin` | 停止并撤销能力，保留已加载状态与配置 |
| `enable(name)` | `InstalledPlugin` | 从 `DISABLED` / `INITIALIZED` / `STOPPED` 恢复；重新注册能力与默认项 |
| `reload(name)` | `InstalledPlugin` | 从原来源重新读取并原子替换，生成新的 `generation` |
| `update(name, source, *, config=None)` | `InstalledPlugin` | 用新包/新版本原子替换 |
| `shutdown()` | `None` | 逆序卸载全部插件，单点失败不影响其余 |
| `start_all()` / `stop_all()` | `None` | 批量启动 / 停止，启动失败会回滚已启动部分 |

`reload` 与 `update` 要求新包声明的 `name` 与目标一致，否则抛 `PluginValidationError`；`update` 未提供 `config` 时沿袭旧配置；目标为 `DISABLED` 时更新后保持禁用状态。

### 查询

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `get(name)` | `InstalledPlugin \| None` | 访问内部记录（`plugin` / `context` / `state` / `generation` / `metrics`） |
| `installed_names()` | `tuple[str, ...]` | 已加载插件名 |
| `running_names()` | `tuple[str, ...]` | 处于 `RUNNING` 的插件名 |
| `status()` | `list[PluginStatus]` | 全量状态快照（含资源占用） |
| `status_of(name)` | `PluginStatus \| None` | 单插件状态 |
| `metrics(name)` | `dict \| None` | 单插件指标字典 |
| `process_usage()` | `dict` | 进程级 CPU / 追踪内存 |
| `describe()` | `list[dict]` | `status()` 的 JSON 友好形式 |

---

## 原子性与回滚

每次热插拔是一个事务：

1. **前置校验**：清单、宿主兼容性、名称唯一性、依赖与版本约束；失败时无副作用。
2. **快照**：捕获 `ComponentRegistry.snapshot_state()` 与 `LifecycleEngine.snapshot_subscriptions()`。
3. **执行**：安装能力 → 订阅 Hook → 初始化 → 启动。
4. **失败回滚**：
   - 调用失败实例的 `on_destroy` / `uninstall`（尽力而为）；
   - 恢复注册表与生命周期订阅快照；
   - 释放沙箱命名空间与暂存目录；
   - 批量安装会逆序卸载已成功的部分；
   - 发出 `PLUGIN_ROLLED_BACK`。

重载 / 更新的回滚更严格：旧实例在停止前先快照，若新实例任一阶段失败，则恢复旧实例的注册表与订阅，并在旧实例原本运行时重启它；只有重启也失败才标记为 `FAILED`。

禁用 / 启用同样使用快照：启用失败会恢复原状态（包括门面活性与能力集合），不会出现“能力只注册了一半”的情况。

并发语义：所有变更方法共享同一把 `asyncio.Lock`；并发加载同一插件时，后到者会因“已安装”被拒绝或在前者完成后按幂等路径返回，不会产生重复注册。

---

## 状态监控

`PluginState` 取值：

`registered`、`initializing`、`initialized`、`starting`、`running`、`stopping`、`stopped`、`disabled`、`destroying`、`destroyed`、`failed`。

`PluginStatus` 字段：

| 字段 | 说明 |
| --- | --- |
| `name` / `version` / `description` / `source` | 身份与来源 |
| `state` | `PluginState` |
| `enabled` | 是否处于活跃状态（含 `initialized`/`running` 等） |
| `healthy` | 非 `FAILED` 且无错误计数 |
| `running` | 是否为 `RUNNING` |
| `requires` / `provides` | 依赖与声明能力 |
| `capabilities` | 实际注册的能力标签（如 `tool:hotplug_greet`） |
| `hooks` | 已订阅 Hook 数量 |
| `uptime_seconds` | 运行时长 |
| `sandbox` | 命名空间与有效策略 |
| `metrics` | `stage_seconds` / `cpu_seconds` / `hook_invocations` / `errors` / `last_error` / 时间戳 |
| `memory_bytes` | 相对安装基线的追踪内存增量（需开启采样） |

资源采样说明：

- CPU 使用 `time.process_time()`，在生命周期阶段与 Hook 执行前后累计；
- 内存使用 `tracemalloc` 做**进程级近似**，无法精确归因到单个插件；`memory_bytes` 为当前追踪值与安装基线的差值；
- 开启方式：`HarnessConfig(plugin_resource_monitoring=True)` 或 `PluginManager(..., resource_monitoring=True)`。

`PluginStatus.to_dict()` 输出可直接用于 HTTP 监控端点：

```python
import json

payload = [item.to_dict() for item in harness.plugins.status()]
print(json.dumps(payload, indent=2, ensure_ascii=False))
```

---

## Harness 集成与配置

`Harness.setup()` 会安装内置插件、按 `plugin_modules` 加载引用、按需发现 entry points，并扫描 `plugin_directories`；这些阶段均为 `autostart=False`。`Harness.start()` 在进入 `RUNNING` 前调用 `plugins.start_all()`，失败则 `stop_all()` 并向上抛出。

相关配置（`HarnessConfig`）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `plugin_modules` | `[]` | 启动时加载的模块引用 |
| `plugin_config` | `{}` | 按插件名分组的配置 |
| `discover_entry_points` | `False` | 是否从 entry points 发现 |
| `entry_point_group` | `"chatanything.plugins"` | entry point 组名 |
| `plugin_directories` | `[]` | 启动时扫描的插件包目录列表 |
| `plugin_resource_monitoring` | `False` | 是否启用 `tracemalloc` 资源采样 |

`setup()` 失败时会调用 `plugins.shutdown()` 清理已安装插件，再进入 `FAILED`。

---

## 完整示例

### 1. 开发一个可热插拔插件

目录 `examples/plugins/hotplug_greet/`：

`plugin.json`

```json
{
  "name": "example.hotplug_greet",
  "version": "1.0.0",
  "entry": "plugin.py:GreetPlugin",
  "provides": ["tool"],
  "priority": 30,
  "sandbox": {"allow_network": false, "allow_subprocess": false}
}
```

`plugin.py`

```python
from chatanything import Capability, Plugin, PluginContext, PluginMeta, ToolSpec


class GreetTool:
    spec = ToolSpec(
        name="hotplug_greet",
        description="Greet a person by name.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )

    async def invoke(self, arguments, context) -> str:
        return f"Hello, {arguments.get('name', 'world')}! This tool was hot-loaded."


class GreetPlugin(Plugin):
    meta = PluginMeta(
        name="example.hotplug_greet",
        version="1.0.0",
        provides=("tool",),
        priority=30,
    )

    async def install(self, context: PluginContext) -> None:
        self.tool = GreetTool()
        context.container.register(
            Capability.TOOL,
            self.tool.spec.name,
            self.tool,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )

    async def on_initialize(self, context: PluginContext) -> None:
        context.logger.info("initialized")

    async def on_start(self, context: PluginContext) -> None:
        context.logger.info("started")

    async def on_stop(self, context: PluginContext) -> None:
        context.logger.info("stopped")

    async def on_destroy(self, context: PluginContext) -> None:
        context.logger.info("destroyed")
```

### 2. 运行期加载、禁用、启用、重载、卸载

```python
import asyncio
from pathlib import Path

from chatanything import Capability, Harness, HarnessConfig
from chatanything.plugins.interfaces import ToolContext

PLUGIN_DIR = Path("examples/plugins/hotplug_greet")
NAME = "example.hotplug_greet"


async def main() -> None:
    harness = Harness(HarnessConfig(log_level=40, plugin_resource_monitoring=True))
    await harness.setup()
    await harness.start()

    # 加载
    record = await harness.plugins.load(PLUGIN_DIR)
    print(record.meta.name, record.state.value)          # example.hotplug_greet running

    tool = harness.container.resolve(Capability.TOOL, "hotplug_greet")
    print(await tool.invoke({"name": "Ada"}, ToolContext(runtime=harness.runtime)))

    # 禁用：能力被撤销，插件仍在内存中
    await harness.plugins.disable(NAME)
    assert not harness.container.contains(Capability.TOOL, "hotplug_greet")

    # 启用：能力与默认项恢复
    await harness.plugins.enable(NAME)
    assert harness.container.contains(Capability.TOOL, "hotplug_greet")

    # 重载：同一目录重新加载，生成新实例
    new_record = await harness.plugins.reload(NAME)
    print("generation:", new_record.generation)

    # 卸载：代码、能力、Hook、命名空间全部清理
    await harness.plugins.unload(NAME)

    await harness.stop()


asyncio.run(main())
```

运行：

```bash
python examples/hotplug_demo.py
```

### 3. 批量加载与依赖排序

```python
records = await harness.plugins.load_many(
    [
        "dist/plugin_b.zip",          # 依赖 example.hotplug_greet
        "plugins/hotplug_greet",      # 被依赖
    ]
)
print([item.meta.name for item in records])   # 依赖在前
```

### 4. 远程更新

```python
from chatanything import RemoteSource

await harness.plugins.update(
    "example.hotplug_greet",
    RemoteSource(
        "https://example.com/plugins/hotplug_greet-1.1.0.zip",
        checksum="sha256:<sha256-hex>",
    ),
    config={"greeting": "Hi"},
)
```

更新失败（清单不合法、校验和不匹配、启动异常）时，旧版本会继续以原状态运行。

### 5. 打包为插件包

```bash
# 目录结构：hotplug_greet/plugin.json + hotplug_greet/plugin.py
zip -r dist/hotplug_greet.zip hotplug_greet
# 计算校验和
sha256sum dist/hotplug_greet.zip
```

---

## 单元测试

热插拔测试位于 `tests/test_plugin_hotplug.py`，覆盖：

- 版本比较与 `~=` 语义；
- 标准生命周期加载与卸载；
- 重复加载拒绝；
- 非法包 / 启动失败容错；
- 批量安装失败回滚；
- 禁用/启用时能力与默认项恢复；
- `reload`、`update` 的版本升级与失败回滚；
- 依赖顺序、缺失依赖、版本冲突、循环依赖；
- 归档与 `file://` 远程来源的校验和；
- 目录扫描；
- 沙箱导入拦截与模块命名空间隔离；
- 并发加载同一插件；
- 监控快照与 Hook 错误归属。

运行：

```bash
python -m pytest tests/test_plugin_hotplug.py -q
python -m pytest -q          # 全量
```

---

## 安全说明

- **协作式沙箱**：导入守卫与注册表门面能防止意外的模块冲突、跨插件写入与超时拖垮主程序，但无法阻止插件通过文件系统、反射或已授权模块执行任意操作。
- **不可信插件**：请放到独立进程或容器中运行，仅通过网络/消息协议与主程序交互。
- **远程来源**：始终提供 `checksum`，并在受控网络内下载；默认 50 MB 上限与 20 秒超时可按需调整。
- **归档解压**：已拒绝路径穿越与符号链接；仍建议先落盘到临时目录再加载，避免长期保留大包。
- **配置隔离**：插件拿到的是只读配置映射；敏感凭据应由宿主通过 `plugin_config` 注入，而不是让插件自行读取环境变量。
- **超时设置**：为不可信或重型插件设置较小的 `max_hook_seconds`；Hook 中阻塞式 I/O 会占用调用线程，建议改为异步实现。
