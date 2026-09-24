# FastAPI 插件内核（分层配置 + 可插拔装配）

在 FastAPI 模板上实现的两项核心能力：

1. **分层配置加载**：多来源按固定优先级合并，冲突可解释，非法输入明确拒绝。
2. **可插拔插件装配**：按声明依赖拓扑装配，失败隔离，生命周期一次性，并发安全。

## 配置优先级

来源按固定优先级合并，数值越大优先级越高（`app/config/sources.py`）：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 0 | `default` | `FieldSpec` 中声明的缺省值 |
| 10 | `file(...)` | JSON 配置文件 |
| 20 | `env(APP_*)` | 环境变量（值按 JSON 解析还原类型） |
| 30 | 外部覆盖 | 启动参数 / 测试注入 |

**冲突可解释**：每个字段解析为 `ResolvedValue`，记录最终生效的 `source`，
以及全部被覆盖的 `(来源, 值)` 列表（`overridden`），可通过 `GET /config` 查询。

**拒绝语义（不静默回退）**：

- 必填项在所有来源中缺失 → `MissingRequiredError`
- 生效来源的值类型不合法（含 `bool` 被当作 `int`）→ `InvalidTypeError`，
  错误信息中携带来源名称，**不会**回退到默认值
- 两种错误是不同异常类型，调用方可区分处理

## 插件契约

实现 `app/plugins/contract.py` 中的 `Plugin` 协议：

```python
class MyPlugin:
    @property
    def name(self) -> str: ...            # 唯一名称
    @property
    def dependencies(self) -> list[str]: ...  # 依赖的插件名
    def init(self) -> None: ...           # 装配时调用，只生效一次
    def dispose(self) -> None: ...        # 释放时调用，只生效一次
```

## 装配与失败语义

- **依赖解析**：Kahn 拓扑排序，依赖先于依赖者初始化；释放按逆序执行。
- **可区分的拒绝原因**（`app/plugins/errors.py`）：
  - `DuplicateRegistrationError`：同名插件重复注册
  - `MissingDependencyError`：依赖未注册（携带缺失列表）
  - `CyclicDependencyError`：依赖成环（携带具体环路径）
  - `PluginInitError`：单个插件 init 抛错（包装原始异常）
- **失败隔离**：单个插件 init 失败被标记为 `FAILED` 并保留原始错误信息，
  其余插件继续装配，服务整体可用；失败状态可通过
  `GET /plugins` / `GET /plugins/{name}` 查询。
- **一次性与重试**：`init`/`dispose` 成功后各只生效一次，重复触发幂等跳过；
  失败的插件可在下次 `assemble()` 时重试，已成功的不会重复初始化，
  不会出现部分生效叠加或资源泄漏。
- **并发安全**：所有状态变更在锁内完成，`statuses()` 返回不可变快照，
  并发装配与并发查询结果一致。

## API

| 端点 | 说明 |
|---|---|
| `GET /config` | 每个配置项的最终值、生效来源与被覆盖记录 |
| `GET /plugins` | 全部插件的装配状态快照（含失败原因） |
| `GET /plugins/{name}` | 单个插件状态 |

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试（日志打印输入、装配顺序与判定依据）
uv run uvicorn main:app # 启动服务，访问 /config 与 /plugins
```

测试覆盖：配置冲突与来源追溯、必填缺失、类型非法（含 bool/int 区分）、
循环依赖、缺失依赖、重复注册、单插件失败隔离与重试一致性、
并发装配只生效一次、并发读取快照一致、并发装配/释放无部分生效。
