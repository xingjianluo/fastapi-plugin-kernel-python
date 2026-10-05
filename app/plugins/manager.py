"""运行期热重载：不重启进程地增/换/删插件并当场重新装配。

语义保证：
- 一整轮 reload 在锁内完成，对外发布的快照原子切换：
  任意时刻读到的都是某一整轮的完整结果，不会读到装配到一半的中间态；
- 未变化且已初始化的插件原样保留，不会重复 init；
- 被移除/替换的插件先完整释放（dispose 报错不阻塞其它插件，错误留痕），
  释放后即从运行态中彻底清除，下一轮不会把已卸载的残留带回来；
- 某插件装配失败时，直接/间接依赖它的插件标记为 BLOCKED 并指明牵连来源，
  上游修复后下一轮 reload 自动恢复。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from app.plugins.assembler import PluginState, PluginStatus
from app.plugins.contract import Plugin
from app.plugins.errors import (
    CyclicDependencyError,
    MissingDependencyError,
    PluginInitError,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DisposeFailure:
    """卸载阶段某个插件释放资源失败的留痕。"""

    name: str
    error: str


@dataclass(frozen=True)
class ReloadReport:
    """一轮热重载的人可读变更报告。"""

    started: tuple[str, ...] = ()        # 本轮新启动成功的插件
    replaced: tuple[str, ...] = ()       # 本轮被替换（旧实例已释放）的插件
    removed: tuple[str, ...] = ()        # 本轮被下线的插件
    kept: tuple[str, ...] = ()           # 未变化、继续服役的插件
    failed: tuple[PluginStatus, ...] = ()    # 本轮装配失败（含原因）
    blocked: tuple[PluginStatus, ...] = ()   # 被上游牵连不可用（含来源）
    dispose_failures: tuple[DisposeFailure, ...] = ()  # 卸载阶段的释放错误


class _Runtime:
    """单个插件的运行态（内部使用，不对外发布）。"""

    __slots__ = ("plugin", "status")

    def __init__(self, plugin: Plugin, status: PluginStatus) -> None:
        self.plugin = plugin
        self.status = status


class PluginManager:
    """持有插件的注册态与运行态，支持运行期热重载。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._registered: dict[str, Plugin] = {}
        self._runtimes: dict[str, _Runtime] = {}
        self._init_order: list[str] = []
        self._snapshot: tuple[PluginStatus, ...] = ()
        self._last_report: ReloadReport = ReloadReport()

    # ---------- 热重载 ----------

    @staticmethod
    def _topo_order(plugins: dict[str, Plugin]) -> list[str]:
        """拓扑排序；缺失依赖的边忽略（装配阶段单独标记），循环依赖抛错。"""
        indegree = {n: 0 for n in plugins}
        dependents: dict[str, list[str]] = {n: [] for n in plugins}
        for name, plugin in plugins.items():
            for dep in plugin.dependencies:
                if dep not in plugins:
                    continue  # 缺失依赖在装配阶段标记为 FAILED，不阻断排序
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
            remaining = sorted(n for n, d in indegree.items() if d > 0)
            raise CyclicDependencyError(remaining)
        return order

    def reload(
        self,
        add: list[Plugin] | None = None,
        remove: list[str] | None = None,
    ) -> ReloadReport:
        """应用一轮变更（新增/替换/移除）并当场重新装配。

        整轮在锁内完成：先卸载（移除/替换的插件，逆初始化序，单个释放报错
        不阻塞其它插件），再按拓扑序装配，最后原子切换对外快照并返回报告。
        循环依赖导致本轮中止，已发布快照保持不变。
        """
        add = list(add or [])
        remove = list(remove or [])
        with self._lock:
            log.info("reload 输入: add=%s remove=%s", [p.name for p in add], remove)
            for p in add:
                if not p.name:
                    raise ValueError("插件名称不能为空")

            old = self._registered
            desired = dict(old)
            removed = tuple(n for n in remove if n in old)
            for n in removed:
                desired.pop(n)
            replaced = tuple(
                p.name for p in add if p.name in old and old[p.name] is not p
            )
            for p in add:
                desired[p.name] = p

            # 循环依赖：本轮中止，已发布快照保持上一整轮的完整结果
            order = self._topo_order(desired)

            # ---- 卸载阶段：先释放被移除/替换的插件（逆初始化序）----
            to_dispose = set(removed) | set(replaced)
            disposed: list[str] = []
            dispose_failures: list[DisposeFailure] = []
            for name in reversed(self._init_order):
                if name not in to_dispose:
                    continue
                rt = self._runtimes.get(name)
                if rt is None or rt.status.state is not PluginState.INITIALIZED:
                    continue
                try:
                    rt.plugin.dispose()
                    disposed.append(name)
                except Exception as exc:  # 隔离：单个释放失败不阻塞其它插件
                    dispose_failures.append(DisposeFailure(name, repr(exc)))
                    log.error("卸载: %s 释放失败（不阻塞其它插件）: %r", name, exc)
            log.info("卸载顺序=%s（逆初始化序）", disposed)
            # 无论释放是否报错，运行态都彻底清除：下一轮从干净状态开始，
            # 不会留下半释放的残留，也不会把已卸载的插件带回来
            for name in to_dispose:
                self._runtimes.pop(name, None)
            self._init_order = [n for n in self._init_order if n not in to_dispose]

            # ---- 装配阶段：按拓扑序逐个判定可用性 ----
            new_statuses: dict[str, PluginStatus] = {}
            initialized_now: list[str] = []
            kept: list[str] = []
            for name in order:
                plugin = desired[name]
                rt = self._runtimes.get(name)
                if (
                    rt is not None
                    and rt.plugin is plugin
                    and rt.status.state is PluginState.INITIALIZED
                ):
                    new_statuses[name] = rt.status
                    kept.append(name)
                    log.info("装配: %s 未变化且已初始化，原样保留（不重复 init）", name)
                    continue

                missing = [d for d in plugin.dependencies if d not in desired]
                if missing:
                    err = MissingDependencyError(name, missing)
                    new_statuses[name] = PluginStatus(
                        name, PluginState.FAILED, error=repr(err)
                    )
                    log.info("装配: %s 不可用，判定依据: 缺失依赖 %s", name, missing)
                    continue

                unavailable = [
                    d
                    for d in plugin.dependencies
                    if new_statuses[d].state is not PluginState.INITIALIZED
                ]
                if unavailable:
                    reason = "; ".join(
                        f"'{d}'({new_statuses[d].state.value}: {new_statuses[d].error})"
                        for d in unavailable
                    )
                    new_statuses[name] = PluginStatus(
                        name,
                        PluginState.BLOCKED,
                        error=f"被上游插件牵连不可用: {reason}",
                        blocked_by=tuple(unavailable),
                    )
                    log.info(
                        "装配: %s 不可用，判定依据: 直接上游 %s 不可用", name, unavailable
                    )
                    continue

                try:
                    plugin.init()
                except Exception as exc:  # 隔离：单个失败不影响其余插件
                    err = PluginInitError(name, exc)
                    new_statuses[name] = PluginStatus(
                        name, PluginState.FAILED, error=repr(err)
                    )
                    log.info("装配: %s 不可用，判定依据: init 抛错 %r", name, exc)
                    continue
                new_statuses[name] = PluginStatus(name, PluginState.INITIALIZED)
                initialized_now.append(name)
                log.info("装配: %s 初始化完成", name)

            # 重建运行态与初始化顺序（保持历史顺序，新初始化的追加在尾部）
            self._runtimes = {
                name: _Runtime(desired[name], new_statuses[name]) for name in order
            }
            self._init_order = [
                n
                for n in self._init_order
                if n in new_statuses
                and new_statuses[n].state is PluginState.INITIALIZED
            ] + initialized_now

            # ---- 原子发布：快照一次性切换，读者不会看到中间态 ----
            self._registered = desired
            self._snapshot = tuple(new_statuses[n] for n in order)
            report = ReloadReport(
                started=tuple(n for n in initialized_now if n not in replaced),
                replaced=replaced,
                removed=removed,
                kept=tuple(kept),
                failed=tuple(
                    s for s in new_statuses.values() if s.state is PluginState.FAILED
                ),
                blocked=tuple(
                    s for s in new_statuses.values() if s.state is PluginState.BLOCKED
                ),
                dispose_failures=tuple(dispose_failures),
            )
            self._last_report = report
            log.info(
                "reload 报告: started=%s replaced=%s removed=%s kept=%s "
                "failed=%s blocked=%s dispose_failures=%s",
                report.started,
                report.replaced,
                report.removed,
                report.kept,
                [(s.name, s.error) for s in report.failed],
                [(s.name, s.blocked_by) for s in report.blocked],
                [(f.name, f.error) for f in report.dispose_failures],
            )
            return report

    # ---------- 状态查询 ----------

    def statuses(self) -> list[PluginStatus]:
        """返回当前已发布快照（某一整轮的完整结果）。"""
        with self._lock:
            return list(self._snapshot)

    def status(self, name: str) -> PluginStatus | None:
        with self._lock:
            for s in self._snapshot:
                if s.name == name:
                    return s
            return None

    def last_report(self) -> ReloadReport:
        with self._lock:
            return self._last_report

    # ---------- 关停 ----------

    def shutdown(self) -> None:
        """释放全部已初始化插件（逆初始化序），并清空到干净状态。

        单个插件释放报错不阻塞其它插件，错误留痕在最后一轮报告中。
        """
        with self._lock:
            failures: list[DisposeFailure] = []
            names = [n for n in reversed(self._init_order)]
            for name in names:
                rt = self._runtimes.get(name)
                if rt is None or rt.status.state is not PluginState.INITIALIZED:
                    continue
                try:
                    rt.plugin.dispose()
                except Exception as exc:  # 隔离：不阻塞其它插件释放
                    failures.append(DisposeFailure(name, repr(exc)))
                    log.error("shutdown: %s 释放失败（不阻塞其它插件）: %r", name, exc)
            log.info("shutdown 卸载顺序=%s dispose_failures=%s",
                     names, [(f.name, f.error) for f in failures])
            self._runtimes.clear()
            self._init_order.clear()
            removed = tuple(s.name for s in self._snapshot)
            self._registered = {}
            self._snapshot = ()
            self._last_report = ReloadReport(
                removed=removed, dispose_failures=tuple(failures)
            )
