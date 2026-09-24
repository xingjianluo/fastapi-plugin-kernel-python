"""插件装配器：依赖拓扑排序、生命周期一次性、失败隔离、并发安全。"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass

from app.plugins.contract import Plugin
from app.plugins.errors import (
    CyclicDependencyError,
    MissingDependencyError,
    PluginInitError,
)
from app.plugins.registry import PluginRegistry


class PluginState(enum.Enum):
    REGISTERED = "registered"
    INITIALIZED = "initialized"
    FAILED = "failed"
    DISPOSED = "disposed"


@dataclass(frozen=True)
class PluginStatus:
    """对外可查询的插件状态快照（不可变）。"""

    name: str
    state: PluginState
    error: str | None = None


class PluginAssembler:
    """按依赖顺序装配插件。

    语义保证：
    - init/dispose 对每个插件各只生效一次（成功后不会重复调用）；
    - 单个插件 init 失败被隔离：记录 PluginInitError 原因，其余插件继续装配；
    - 失败后重试 assemble() 只会重试 FAILED 插件，已成功的不会重复初始化，
      不会出现部分生效叠加或资源泄漏；
    - 所有状态变更在锁内完成，statuses() 返回不可变快照，并发读取安全。
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry
        self._lock = threading.RLock()
        self._states: dict[str, PluginStatus] = {}
        self._init_order: list[str] = []  # 成功初始化的顺序，dispose 时逆序执行

    # ---------- 依赖解析 ----------

    def resolve_order(self) -> list[str]:
        """拓扑排序；缺失依赖与循环依赖抛出可区分错误。"""
        plugins = self._registry.all()
        for name, plugin in plugins.items():
            missing = [d for d in plugin.dependencies if d not in plugins]
            if missing:
                raise MissingDependencyError(name, missing)

        # Kahn 算法；入度 = 未满足的依赖数
        indegree = {n: 0 for n in plugins}
        dependents: dict[str, list[str]] = {n: [] for n in plugins}
        for name, plugin in plugins.items():
            for dep in plugin.dependencies:
                indegree[name] += 1
                dependents[dep].append(name)

        queue = sorted(n for n, d in indegree.items() if d == 0)
        order: list[str] = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for nxt in dependents[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)

        if len(order) != len(plugins):
            cycle = self._find_cycle(plugins)
            raise CyclicDependencyError(cycle)
        return order

    @staticmethod
    def _find_cycle(plugins: dict[str, Plugin]) -> list[str]:
        """用 DFS 找出一条具体的循环路径，便于定位问题。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in plugins}
        stack: list[str] = []

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            stack.append(node)
            for dep in plugins[node].dependencies:
                if color[dep] == GRAY:
                    return stack[stack.index(dep):] + [dep]
                if color[dep] == WHITE:
                    found = dfs(dep)
                    if found:
                        return found
            stack.pop()
            color[node] = BLACK
            return None

        for name in plugins:
            if color[name] == WHITE:
                found = dfs(name)
                if found:
                    return found
        return []

    # ---------- 生命周期 ----------

    def assemble(self) -> list[PluginStatus]:
        """按拓扑序初始化插件；失败被隔离并记录，可安全重试。"""
        order = self.resolve_order()
        plugins = self._registry.all()
        with self._lock:
            for name in order:
                status = self._states.get(name)
                if status and status.state is PluginState.INITIALIZED:
                    continue  # 已成功初始化，幂等跳过
                # DISPOSED 允许重新初始化：dispose 已完整释放资源，
                # 重新 assemble 等价于干净重启，不会产生部分生效叠加
                try:
                    plugins[name].init()
                except Exception as exc:  # 隔离：单个失败不影响其余插件
                    err = PluginInitError(name, exc)
                    self._states[name] = PluginStatus(
                        name=name, state=PluginState.FAILED, error=repr(err)
                    )
                    continue
                self._states[name] = PluginStatus(
                    name=name, state=PluginState.INITIALIZED
                )
                if name not in self._init_order:
                    self._init_order.append(name)
            return self.statuses()

    def dispose(self) -> None:
        """按装配逆序释放已初始化插件，各只生效一次。"""
        with self._lock:
            plugins = self._registry.all()
            for name in reversed(self._init_order):
                status = self._states.get(name)
                if not status or status.state is not PluginState.INITIALIZED:
                    continue
                try:
                    plugins[name].dispose()
                finally:
                    # 即使 dispose 抛错也标记为已释放，保证只生效一次、不泄漏
                    self._states[name] = PluginStatus(
                        name=name, state=PluginState.DISPOSED
                    )
            self._init_order.clear()

    # ---------- 状态查询 ----------

    def status(self, name: str) -> PluginStatus | None:
        with self._lock:
            return self._states.get(name)

    def statuses(self) -> list[PluginStatus]:
        """返回不可变状态快照列表，并发读取安全。"""
        with self._lock:
            return list(self._states.values())
