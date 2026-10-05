# FastAPI 插件内核（分层配置 + 可插拔装配 + 运行期热重载）

在 FastAPI 模板上实现的三项核心能力：

1. **分层配置加载**：多来源按固定优先级合并，冲突可解释，非法输入明确拒绝。
2. **可插拔插件装配**：按声明依赖拓扑装配，失败隔离，生命周期一次性，并发安全。
3. **运行期热重载**：不重启进程地登记/替换/移除插件并当场重新装配，
   每轮产出入可读的变更报告。

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

`app/plugins/reloader.py` 的 `PluginReloader.reload(plugins)` 用一组目标插件
**整体替换**当前注册表并当场重新装配，无需重启进程。服务内通过
`POST /reload` 触发（从 `app.state.plugin_provider` 取目标集合）。

**变更报告**：每轮重装返回 `ReloadReport`（同时可通过 `GET /reload/report`
查询最近一轮），按插件逐个给出分类与人可读摘要（`summary()`）：

| 分类 | 含义 |
|---|---|
| `started` | 本轮新起来（新登记，或上一轮失败/被阻断后恢复） |
| `replaced` | 同名新实例替换并初始化成功 |
| `restarted` | 因上游被替换/移除而被迫重启的保留插件 |
| `removed` | 被下线并释放 |
| `unchanged` | 未受影响，保持运行（**不会**被重复初始化） |
| `failed` | 装配失败（init 抛错 / 缺依赖 / 依赖成环），附原始错误 |
| `blocked` | 被失败的上游牵连，原因中指明直接上游与根因插件 |

**一致性与并发语义**：

- 一整轮重装的状态表在**一次锁临界区内原子切换**：任何时刻查询到的都是
  某一整轮的完整结果，不会读到装配到一半的中间态；
- 未受影响的已初始化插件不重复启动；被移除插件的状态彻底清除，
  后续轮次不会把它带回来；每轮状态整体重建，反复重装不累积残留；
- 多线程并发 `reload()` 由重载锁串行化。

**失败传播与恢复**：某插件装配失败（或缺依赖、依赖成环）时，所有直接或
间接依赖它的插件标记为 `blocked` 且不尝试初始化，报告与
`GET /plugins/{name}` 中可见牵连它的上游与根因；上游修复后下一轮重装，
受牵连插件自动恢复为可用。

**卸载容错**：下线/替换时按装配逆序释放；单个插件 `dispose` 报错不会卡住
其它插件的释放，错误记入报告的 `dispose_errors`（进程退出时
`assembler.dispose()` 同样记录到插件状态的 `error` 字段）。一轮卸载后再
重装从干净状态开始，无半释放残留。

**兼容范围**：热重载只做增量——`GET /config`、`GET /plugins`、
`GET /plugins/{name}` 的返回结构与语义不变（仅可能多出 `blocked` 这一
新状态值）；原有装配器 `assemble()/dispose()` 行为与一次性保证不变。

## API

| 端点 | 说明 |
|---|---|
| `GET /config` | 每个配置项的最终值、生效来源与被覆盖记录 |
| `GET /plugins` | 全部插件的装配状态快照（含失败/被牵连原因） |
| `GET /plugins/{name}` | 单个插件状态 |
| `POST /reload` | 从插件提供者取目标集合，当场热重载并返回变更报告 |
| `GET /reload/report` | 最近一轮热重载的变更报告 |

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试（日志打印每轮重载的输入、装配/卸载顺序与判定依据）
uv run uvicorn main:app # 启动服务，访问 /config 与 /plugins
```

热重载手工验证：

```bash
curl -X POST localhost:8000/reload   # 触发一轮热重载，返回变更报告
curl localhost:8000/reload/report    # 查看最近一轮报告
curl localhost:8000/plugins          # 老查询方式照常可用
```

测试覆盖：配置冲突与来源追溯、必填缺失、类型非法（含 bool/int 区分）、
循环依赖、缺失依赖、重复注册、单插件失败隔离与重试一致性、
并发装配只生效一次、并发读取快照一致、并发装配/释放无部分生效；
热重载的增/换/删与报告分类、未受影响插件不重启、移除后状态清除不复活、
失败沿依赖链传播与修复后恢复、替换上游触发下游重启、释放报错隔离与记录、
卸载后干净重装、重复重装幂等收敛、并发重装与读取只见整轮快照、
老查询接口兼容。
