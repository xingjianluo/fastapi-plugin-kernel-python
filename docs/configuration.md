# 分层配置加载

## 来源与固定优先级

`LayeredConfigLoader.load(sources)` 接收一个有序来源列表，**列表顺序即优先级顺序（高 → 低）**：

```
external（外部覆盖，如 CLI / 下发文件）
        ↓ 被覆盖
env（环境变量映射，可选）
        ↓ 被覆盖
builtin（ConfigSchema 中每个 ConfigField 声明的 default，最低优先级）
```

- 同一个键在多个来源出现时，**排在最前的来源胜出**，其余来源记录在
  `MergedConfig.shadowed[key]` 中，不丢信息。
- `MergedConfig.explain(key)` 返回自然语言判定依据：
  生效来源、其在优先级中的位次、被覆盖的低优先级来源，
  或“未在任何来源提供，使用声明缺省值”。
- 来源名称不允许重复（直接 `ValueError`），避免优先级出现歧义。

## 类型校验：不静默回退

- 支持类型：`str / int / float / bool`，**严格校验，不做隐式字符串转换**；
  `bool` 不被当作 `int`（`True` 对 `int` 项非法，`1` 对 `bool` 项非法）。
- **生效来源给出的值类型不合法时，整体拒绝加载**（`InvalidTypeError`），
  不会悄悄退回低优先级来源或缺省值。错误中带 `key / expected / value / source`。
- **必填项在所有来源都缺失时拒绝**（`MissingRequiredError`），带 `key` 与已检查来源列表。
- 未在 schema 中声明的键原样透传（无法做类型校验），供扩展使用。

两类错误都继承 `ConfigError`，调用方可按异常类型区分拒绝原因：

| 拒绝原因 | 异常 | 关键字段 |
| --- | --- | --- |
| 必填缺失 | `MissingRequiredError` | `key`, `sources` |
| 类型不合法 | `InvalidTypeError` | `key`, `expected`, `value`, `source` |

## 用法

```python
from app.config import ConfigField, ConfigSchema, LayeredConfigLoader

schema = ConfigSchema(fields=(
    ConfigField("app.name", "str", required=True),
    ConfigField("http.port", "int", default=8080),
))
cfg = LayeredConfigLoader(schema).load([
    ("external", {"http.port": 9000}),   # 高优先级
    ("file",     {"app.name": "x", "http.port": 8000}),
])
assert cfg.get("http.port") == 9000
print(cfg.explain("http.port"))
# 配置项 'http.port' 生效来源='external'（优先级 1/2，高 -> 低）；
# 被其覆盖的低优先级来源=['file']；声明缺省值未使用
```

> 环境变量的值恒为字符串，仅适合 `str` 配置项；`int/float/bool` 应由结构化的
> external 覆盖提供（由 CLI/文件解析层先完成类型转换）。
