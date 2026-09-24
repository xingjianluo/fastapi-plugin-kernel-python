"""不可变装配结果与失败状态记录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class PluginState(str, Enum):
    REGISTERED = "registered"
    INITIALIZED = "initialized"
    FAILED = "failed"
    SKIPPED = "skipped"
    TORN_DOWN = "torn_down"


@dataclass(frozen=True)
class FailureRecord:
    """单个插件的失败记录，保留原始错误。"""

    plugin: str
    error_type: str
    message: str
    original_error: BaseException | None = None


@dataclass(frozen=True)
class AssemblyResult:
    """一次装配的不可变快照。"""

    order: tuple[str, ...] = ()
    initialized: tuple[str, ...] = ()
    failed: tuple[FailureRecord, ...] = ()
    skipped: tuple[str, ...] = ()
    instances: Mapping[str, Any] = field(default_factory=dict)

    def is_healthy(self, name: str) -> bool:
        """插件是否在本快照中处于成功初始化状态。"""
        return name in self.initialized and name in self.instances

    def failure_of(self, name: str) -> FailureRecord | None:
        for rec in self.failed:
            if rec.plugin == name:
                return rec
        return None


__all__ = ["PluginState", "FailureRecord", "AssemblyResult"]
