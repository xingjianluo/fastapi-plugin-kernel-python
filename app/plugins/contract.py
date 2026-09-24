"""插件契约：声明式描述与生命周期接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Plugin(ABC):
    """插件实现的生命周期契约。

    initialize / teardown 由管理器保证各只被调用一次，
    即使在并发请求或失败重试场景下。
    """

    name: str
    dependencies: tuple[str, ...] = ()

    @abstractmethod
    async def initialize(self, ctx: "PluginContext") -> None:
        """获取依赖实例并执行初始化。"""

    async def teardown(self) -> None:
        """释放资源；默认无资源可释放。initialize 未成功时不会被调用。"""


class PluginContext(ABC):
    """初始化期上下文：只读访问已就绪的依赖。"""

    @abstractmethod
    def require(self, name: str) -> Any:
        """按名获取已成功初始化的依赖实例。"""


class PluginSpec:
    """插件声明（名称 + 依赖 + 工厂）。"""

    def __init__(
        self,
        name: str,
        factory: Any,
        dependencies: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.factory = factory
        self.dependencies = tuple(dependencies)


__all__ = ["Plugin", "PluginContext", "PluginSpec"]
