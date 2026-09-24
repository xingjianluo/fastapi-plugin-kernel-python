"""分层配置加载：按优先级合并、记录来源、严格校验。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config.errors import InvalidTypeError, MissingRequiredError
from app.config.sources import ConfigSource, PRIORITY_DEFAULT


@dataclass(frozen=True)
class FieldSpec:
    """单个配置字段的契约。"""

    name: str
    type_: type
    required: bool = False
    default: Any = None


@dataclass(frozen=True)
class ResolvedValue:
    """解析后的配置值及其来源，用于冲突解释。"""

    key: str
    value: Any
    source: str
    overridden: tuple[tuple[str, Any], ...]  # 被覆盖的 (来源, 值)，按优先级从低到高


class ConfigLoader:
    """按固定优先级合并多个配置来源。

    语义：
    - 同名字段由 priority 高的来源胜出，被覆盖的值全部记录在 overridden 中；
    - 类型不合法时抛出 InvalidTypeError，绝不静默回退到默认值；
    - 必填项在所有来源中都缺失时抛出 MissingRequiredError。
    """

    def __init__(self, specs: list[FieldSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    def load(self, sources: list[ConfigSource]) -> dict[str, ResolvedValue]:
        ordered = sorted(sources, key=lambda s: s.priority)
        result: dict[str, ResolvedValue] = {}
        for spec in self._specs.values():
            result[spec.name] = self._resolve(spec, ordered)
        return result

    def _resolve(self, spec: FieldSpec, ordered: list[ConfigSource]) -> ResolvedValue:
        # 候选 (来源名, 值)，按优先级从低到高；默认值作为最低优先级候选
        candidates: list[tuple[str, Any]] = []
        if spec.default is not None:
            candidates.append(("default", spec.default))
        for src in ordered:
            if spec.name in src.values:
                candidates.append((src.name, src.values[spec.name]))

        if not candidates:
            raise MissingRequiredError(spec.name)

        source_name, value = candidates[-1]
        if not isinstance(value, spec.type_) or (
            spec.type_ is not bool and isinstance(value, bool) and spec.type_ is int
        ):
            # bool 是 int 的子类，需显式区分，避免 True 被当作 int 接受
            actual = type(value).__name__
            raise InvalidTypeError(spec.name, spec.type_.__name__, actual, source_name)

        overridden = tuple(candidates[:-1])
        return ResolvedValue(
            key=spec.name, value=value, source=source_name, overridden=overridden
        )
