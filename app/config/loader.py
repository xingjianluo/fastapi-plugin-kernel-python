"""多来源、固定优先级的分层配置加载器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config.errors import InvalidTypeError, MissingRequiredError
from app.config.schema import ConfigField, ConfigSchema, ConfigType

_PY_TYPES: dict[ConfigType, str] = {
    "str": "str",
    "int": "int",
    "float": "int/float",
    "bool": "bool",
}


def _check_type(field_: ConfigField, value: Any) -> bool:
    """严格类型检查：不做隐式字符串转换；bool 不被当作 int。"""
    t = field_.type
    if t == "str":
        return isinstance(value, str)
    if t == "bool":
        return isinstance(value, bool)
    if t == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


@dataclass(frozen=True)
class ConfigValue:
    """单个配置值及其解析来源。"""

    key: str
    value: Any
    source: str


@dataclass(frozen=True)
class MergedConfig:
    """合并结果：值、生效来源、被覆盖来源与优先级顺序。"""

    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    shadowed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    priority: list[str] = field(default_factory=list)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def explain(self, key: str) -> str:
        """解释某个配置项为何取当前值（冲突可解释）。"""
        total = len(self.priority)
        if key in self.sources:
            winner = self.sources[key]
            rank = self.priority.index(winner) + 1
            losers = self.shadowed.get(key, ())
            if losers:
                tail = f"；被其覆盖的低优先级来源={list(losers)}；声明缺省值未使用"
            else:
                tail = "；无其他来源提供该项"
            return (
                f"配置项 {key!r} 生效来源={winner!r}"
                f"（优先级 {rank}/{total}，高 -> 低）{tail}"
            )
        if key in self.values:
            return f"配置项 {key!r} 未在任何来源提供，使用声明缺省值={self.values[key]!r}"
        return f"配置项 {key!r} 不存在于 schema 或任何来源"


class LayeredConfigLoader:
    """按固定优先级（高 -> 低）合并多个字典来源。

    - sources 顺序即优先级顺序，前者覆盖后者；
    - schema 中声明的 default 为最低优先级；
    - 高优先级来源的值类型不合法时直接拒绝，不静默回退到低优先级或缺省值；
    - 必填项在所有来源都缺失时拒绝。
    """

    def __init__(self, schema: ConfigSchema) -> None:
        self.schema = schema

    def load(self, sources: list[tuple[str, dict[str, Any]]]) -> MergedConfig:
        names = [name for name, _ in sources]
        if len(names) != len(set(names)):
            raise ValueError(f"配置来源名称重复: {names}")

        # 按高 -> 低遍历，首次出现即胜出，后续来源记入 shadowed。
        raw: dict[str, Any] = {}
        winners: dict[str, str] = {}
        shadowed_tmp: dict[str, list[str]] = {}
        for name, data in sources:
            for key, val in data.items():
                if key in raw:
                    shadowed_tmp.setdefault(key, []).append(name)
                else:
                    raw[key] = val
                    winners[key] = name
        shadowed = {k: tuple(v) for k, v in shadowed_tmp.items()}

        # 逐字段校验/取值。
        values: dict[str, Any] = {}
        for f in self.schema.fields:
            if f.key in raw:
                val = raw[f.key]
                if not _check_type(f, val):
                    raise InvalidTypeError(
                        key=f.key,
                        expected=_PY_TYPES[f.type],
                        value=val,
                        source=winners[f.key],
                    )
                values[f.key] = val
            elif f.required:
                raise MissingRequiredError(key=f.key, sources=names)
            else:
                values[f.key] = f.default

        # schema 未声明的键原样透传（无法做类型校验）。
        for key, val in raw.items():
            if self.schema.get(key) is None:
                values[key] = val

        return MergedConfig(
            values=values,
            sources=winners,
            shadowed=shadowed,
            priority=names,
        )
