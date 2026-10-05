# FastAPI 插件内核（分层配置 + 可插拔装配 + 运行期热重载）

在 FastAPI 模板上实现的三项核心能力：

1. **分层配置加载**：多来源按固定优先级合并，冲突可解释，非法输入明确拒绝。
2. **可插拔插件装配**：按声明依赖拓扑装配，失败隔离，生命周期一次性，并发安全。
3. **运行期热重载**：不重启进程地增/换/删插件并当场重新装配，每轮产出人可读变更报告。

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

## 运行期热重载

`app/plugins/manager.py` 的 `PluginManager` 在装配能力之上提供热重载，
服务运行中调用即可，无需重启进程：

```python
report = manager.reload(
    add=[NewPlugin("v2"), NewPlugin("core")],  # 新名字=登记；同名新实例=替换
    remove=["legacy"],                          # 按名字下线
)
```

**每轮返回 `ReloadReport`（人可读变更报告）**：

| 字段 | 含义 |
|---|---|
| `started` | 本轮新启动成功的插件 |
| `replaced` | 被同名新实例替换的插件（旧实例已释放） |
| `removed` | 本轮下线的插件 |
| `kept` | 未变化、继续服役的插件 |
| `failed` | 本轮没起来的插件及各自原因（init 抛错 / 缺失依赖） |
| `blocked` | 被失败上游牵连而不可用的插件，`blocked_by` 指明直接上游 |
| `dispose_failures` | 卸载阶段释放资源报错的留痕（不阻塞其它插件） |

**一致性与隔离语义**：

- **原子快照**：一整轮 reload 在锁内完成，对外快照一次性切换；
  任意时刻 `statuses()` / `GET /plugins` 读到的都是某一整轮的完整结果，
  不会读到装配到一半的中间态。
- **不重复启动**：未变化且已初始化的插件原样保留（`kept`），不会重复 `init`。
- **不留残留**：被移除/替换的插件先按逆初始化序释放，随后从运行态彻底清除；
  即使 `dispose` 抛错也照样清除（错误记入 `dispose_failures`），
  下一轮 reload 从干净状态开始，已卸载的插件不会被带回来，
  反复重装状态不累积。
- **失败传播与恢复**：某插件装配失败（init 抛错或缺失依赖）时，
  直接/间接依赖它的插件标记为 `blocked` 并在 `error`/`blocked_by` 中
  指明牵连来源；上游修复后再 reload，受牵连的插件自动恢复，
  一直健康的插件不受影响。
- **整轮中止**：新增变更引入循环依赖时抛 `CyclicDependencyError`，
  本轮中止，已发布快照保持上一整轮的完整结果。

**与原有行为的兼容范围**：

- `GET /plugins` / `GET /plugins/{name}` 返回结构不变
  （`name` / `state` / `error` 三个字段），未知插件仍返回 `unknown`；
  被牵连插件的 `state` 为新取值 `blocked`，原因在 `error` 字段中。
- 底层的 `PluginRegistry` / `PluginAssembler` 语义不变，可继续独立使用。

## API

| 端点 | 说明 |
|---|---|
| `GET /config` | 每个配置项的最终值、生效来源与被覆盖记录 |
| `GET /plugins` | 全部插件当前轮的完整状态快照（含失败/被牵连原因） |
| `GET /plugins/{name}` | 单个插件状态 |
| `GET /reload/report` | 最近一轮热重载的变更报告（含卸载错误留痕） |

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试（日志打印每轮重载的输入、装配/卸载顺序、
                        # 每个插件可用或不可用的判定依据）
uv run uvicorn main:app # 启动服务，访问 /config、/plugins 与 /reload/report
```

手动验证热重载（服务运行中，不重启进程）：

```python
from app.plugins.manager import PluginManager
# app.state.plugin_manager 即运行中的管理器
manager.reload(add=[MyPlugin("v2")], remove=["legacy"])
# 随后 GET /plugins 可见新一整轮的完整快照，
# GET /reload/report 可见本轮 started/replaced/removed/kept/failed/blocked
```

测试覆盖：配置冲突与来源追溯、必填缺失、类型非法（含 bool/int 区分）、
循环依赖、缺失依赖、重复注册、单插件失败隔离与重试一致性、
并发装配只生效一次、并发读取快照一致、并发装配/释放无部分生效；
热重载的增/换/删与报告分类、已卸载插件不回流、反复重装幂等、
失败沿依赖链传播（直接+间接）与修复后恢复、缺失依赖隔离、
dispose 报错不阻塞其它插件且留痕可查、下一轮从干净状态开始、
循环依赖整轮中止且旧快照不变、reload 期间并发读者只见完整轮快照、
老查询端点结构兼容。
