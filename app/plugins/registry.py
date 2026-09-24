"""插件注册表：登记插件并拒绝重复注册。"""
from __future__ import annotations

import threading

from app.plugins.contract import Plugin
from app.plugins.errors import DuplicateRegistrationError


class PluginRegistry:
    """保存已注册插件，重复注册立即拒绝（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        name = plugin.name
        if not name:
            raise ValueError("插件名称不能为空")
        with self._lock:
            if name in self._plugins:
                raise DuplicateRegistrationError(name)
            self._plugins[name] = plugin

    def get(self, name: str) -> Plugin | None:
        with self._lock:
            return self._plugins.get(name)

    def all(self) -> dict[str, Plugin]:
        """返回注册表的不可变快照。"""
        with self._lock:
            return dict(self._plugins)
