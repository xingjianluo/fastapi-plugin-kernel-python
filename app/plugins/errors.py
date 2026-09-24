"""插件装配相关的可区分拒绝原因。"""

from __future__ import annotations


class PluginError(Exception):
    """所有插件错误的基类。"""


class DuplicatePluginError(PluginError):
    """重复注册。"""


class MissingDependencyError(PluginError):
    """依赖的插件未注册。"""

    def __init__(self, plugin: str, missing: list[str]) -> None:
        self.plugin = plugin
        self.missing = missing
        super().__init__(
            f"插件依赖缺失: plugin={plugin!r}, missing={missing}"
        )


class CircularDependencyError(PluginError):
    """依赖图存在环。"""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(f"插件循环依赖: cycle={' -> '.join(cycle)}")


class PluginInitError(PluginError):
    """单个插件初始化失败（隔离记录，不拖垮整个服务）。"""

    def __init__(self, plugin: str, cause: BaseException) -> None:
        self.plugin = plugin
        self.cause = cause
        super().__init__(
            f"插件初始化失败: plugin={plugin!r}, error={cause!r}"
        )


__all__ = [
    "PluginError",
    "DuplicatePluginError",
    "MissingDependencyError",
    "CircularDependencyError",
    "PluginInitError",
]
