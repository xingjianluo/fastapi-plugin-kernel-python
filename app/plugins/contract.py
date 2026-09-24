"""插件契约：名称、依赖声明与生命周期钩子。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Plugin(Protocol):
    """插件协议：实现方需提供名称、依赖与 init/dispose 钩子。"""

    @property
    def name(self) -> str: ...

    @property
    def dependencies(self) -> list[str]: ...

    def init(self) -> None:
        """初始化，装配器保证只生效一次。"""
        ...

    def dispose(self) -> None:
        """释放资源，装配器保证只生效一次。"""
        ...
