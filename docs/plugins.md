# 可插拔插件装配

## 插件契约

每个插件继承 `app.plugins.Plugin` 并实现：

```python
class MyPlugin(Plugin):
    name = "my-plugin"                 # 唯一名称
    dependencies = ("other",)          # 声明依赖（先完成初始化的插件名）

    async def initialize(self, ctx: PluginContext) -> None:
        other = ctx.require("other")   # 取已就绪的依赖实例
        # 获取资源；抛异常 = 装配失败

    async def teardown(self) -> None:
        # 释放资源；默认空实现；initialize 未成功的插件不会被调用
```

注册使用工厂：`PluginSpec(name, factory, dependencies=...)`。工厂同步抛错也算装配失败。

## 装配顺序与拒绝语义

`assembly_order()` 用 Kahn 拓扑排序（同层按注册顺序，结果确定），三类拒绝原因可区分：

| 场景 | 异常 | 判定依据 |
| --- | --- | --- |
| 重复注册 | `DuplicatePluginError` | 注册表中名称已存在（`register` 时立即拒绝） |
| 缺失依赖 | `MissingDependencyError` | 某插件依赖的名称不在注册表（带 `plugin` 与 `missing`） |
| 循环依赖 | `CircularDependencyError` | 拓扑排序后仍有未决节点，错误中带具体环路径 |

校验在任何实例化/初始化之前统一进行：**校验不过，不产生任何插件实例**。

## 失败隔离

- 单个插件工厂或 `initialize` 抛错 → 记为 `FAILED`，保留
  `error_type`、`message` 与**原始异常对象** `original_error`；
- 依赖了失败插件（或传递依赖失败）的插件标记为 `SKIPPED`，不会初始化；
- 与失败链无关的插件照常初始化，**服务整体保持可用**；
- 失败状态通过 `manager.result()` 或 HTTP `GET /readyz`、`GET /plugins/{name}` 可查。

`AssemblyResult` 是 frozen dataclass，`instances` 为 `MappingProxyType`，
**装配结果发布后不可变**；并发请求拿到的是同一快照。

## 生命周期与并发

- 并发调用 `assemble()` **共享同一次装配任务**，每个插件的 `initialize` 只执行一次；
- `teardown()` 按初始化**逆序**执行，只释放初始化成功且尚未释放的插件，
  幂等（重复调用不生效），并发调用共享同一次卸载；
- 某个插件 `teardown` 抛错会被记录在 `manager.teardown_errors` 中，
  **不阻断其余插件释放**，避免资源泄漏；
- 失败后重试运行语义：未做任何修复时 `assemble()` 返回同一失败快照（不自动重做）；
  注册表新增声明会使旧快照失效，下次 `assemble()` **整体重建**状态，
  不存在“部分插件已生效”的中间态。同名插件不允许静默覆盖注册，避免替换与装配不一致。

## FastAPI 集成

`create_app(specs, overrides)` 的 lifespan 中自动 `assemble()` / `teardown()`：

| 端点 | 含义 |
| --- | --- |
| `GET /healthz` | 进程存活（恒 ok，插件失败不影响） |
| `GET /readyz` | 装配就绪状态：`ready`/`degraded`，含 order / initialized / failed / skipped |
| `GET /plugins/{name}` | 单插件健康状态与失败的 `error_type`/`message` |
| `GET /config/{key}` | 配置值、生效来源与冲突解释 |

单插件失败时 `/readyz` 返回 `degraded` 但 HTTP 仍为 200，失败详情在响应体中。
