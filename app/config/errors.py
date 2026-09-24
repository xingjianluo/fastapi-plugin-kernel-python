"""配置加载相关的可区分错误类型。"""
from __future__ import annotations


class ConfigError(Exception):
    """配置错误基类。"""


class MissingRequiredError(ConfigError):
    """必填配置项缺失。"""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"必填配置项缺失: {key}")


class InvalidTypeError(ConfigError):
    """配置值类型不合法。"""

    def __init__(self, key: str, expected: str, actual: str, source: str) -> None:
        self.key = key
        self.expected = expected
        self.actual = actual
        self.source = source
        super().__init__(
            f"配置项类型不合法: {key} 期望 {expected} 实际 {actual} (来源: {source})"
        )
