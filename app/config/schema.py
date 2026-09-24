"""配置项声明（键、类型、缺省值、是否必填）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ConfigType = Literal["str", "int", "float", "bool"]


@dataclass(frozen=True)
class ConfigField:
    """单个配置项的声明。"""

    key: str
    type: ConfigType
    required: bool = False
    default: Any = None


@dataclass(frozen=True)
class ConfigSchema:
    """配置项声明集合。"""

    fields: tuple[ConfigField, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if len({f.key for f in self.fields}) != len(self.fields):
            raise ValueError("ConfigSchema 中存在重复的配置键声明")

    def get(self, key: str) -> ConfigField | None:
        for f in self.fields:
            if f.key == key:
                return f
        return None


__all__ = ["ConfigField", "ConfigSchema", "ConfigType"]
