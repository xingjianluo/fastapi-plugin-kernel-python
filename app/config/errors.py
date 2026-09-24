"""配置加载相关的可区分拒绝原因。"""

from __future__ import annotations


class ConfigError(Exception):
    """所有配置错误的基类。"""


class MissingRequiredError(ConfigError):
    """必填项缺失。"""

    def __init__(self, key: str, sources: list[str]) -> None:
        self.key = key
        self.sources = sources
        super().__init__(
            f"必填配置项缺失: key={key!r}, 已检查来源={sources}"
        )


class InvalidTypeError(ConfigError):
    """配置值类型不合法。"""

    def __init__(self, key: str, expected: str, value: object, source: str) -> None:
        self.key = key
        self.expected = expected
        self.value = value
        self.source = source
        super().__init__(
            f"配置类型不合法: key={key!r}, 期望={expected}, "
            f"实际值={value!r}, 来源={source}"
        )


__all__ = [
    "ConfigError",
    "MissingRequiredError",
    "InvalidTypeError",
]
