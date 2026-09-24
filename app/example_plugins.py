"""示例插件：演示如何实现插件契约与依赖声明。

在 create_app(specs=[...]) 中通过 PluginSpec 注册即可被装配。
"""

from __future__ import annotations

from typing import Any

from app.plugins import Plugin, PluginSpec


class CounterPlugin(Plugin):
    """无依赖基础插件：initialize 获取资源，teardown 释放资源。"""

    name = "counter"
    dependencies: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._value: int | None = None

    async def initialize(self, ctx: Any) -> None:
        self._value = 0  # 模拟获取/初始化资源

    async def teardown(self) -> None:
        self._value = None  # 模拟释放资源

    def next(self) -> int:
        assert self._value is not None, "插件尚未初始化"
        self._value += 1
        return self._value


class ReportPlugin(Plugin):
    """依赖 counter 的下游插件。"""

    name = "report"
    dependencies = ("counter",)

    def __init__(self) -> None:
        self._counter: CounterPlugin | None = None

    async def initialize(self, ctx: Any) -> None:
        # 只能拿到已成功初始化的依赖；counter 失败时本插件会被 SKIPPED。
        self._counter = ctx.require("counter")

    async def teardown(self) -> None:
        self._counter = None


SPECS: list[PluginSpec] = [
    PluginSpec("counter", CounterPlugin),
    PluginSpec("report", ReportPlugin, dependencies=("counter",)),
]
