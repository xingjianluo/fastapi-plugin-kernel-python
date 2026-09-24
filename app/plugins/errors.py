"""插件装配相关的可区分错误类型。"""
from __future__ import annotations


class PluginError(Exception):
    """插件错误基类。"""


class DuplicateRegistrationError(PluginError):
    """重复注册同名插件。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"插件重复注册: {name}")


class MissingDependencyError(PluginError):
    """声明的依赖不存在。"""

    def __init__(self, name: str, missing: list[str]) -> None:
        self.name = name
        self.missing = missing
        super().__init__(f"插件 {name} 缺失依赖: {missing}")


class CyclicDependencyError(PluginError):
    """依赖图中存在环。"""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(f"插件依赖存在循环: {' -> '.join(cycle)}")


class PluginInitError(PluginError):
    """单个插件初始化失败（携带原始错误）。"""

    def __init__(self, name: str, cause: BaseException) -> None:
        self.name = name
        self.cause = cause
        super().__init__(f"插件 {name} 初始化失败: {cause!r}")
