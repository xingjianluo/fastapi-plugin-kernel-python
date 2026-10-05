"""运行期热重载：不重启进程地登记/替换/移除插件并当场重新装配。

语义保证：
- 一整轮重装原子可见：状态表在一次锁临界区内整体切换，任何时刻读到的
  都是某一整轮的完整结果，不会读到装配到一半的中间态；
- 未受影响的已初始化插件不重复启动；被移除的插件状态彻底清除，
  不会被后续轮次带回来；每轮状态整体重建，反复重装不累积残留；
- 装配失败沿依赖链传播：直接或间接依赖失败插件的插件标记为 BLOCKED 并
  记录牵连它的上游；上游修复后下一轮重装自动恢复；
- 卸载阶段单个插件 dispose 报错不影响其它插件释放，错误记录在报告中；
  卸载后再重装从干净状态开始，无半释放残留。
"""
from __future__ import annotations

import enum
import logging
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.plugins.assembler import PluginAssembler, PluginState, PluginStatus
from app.plugins.contract import Plugin
from app.plugins.errors import (
    CyclicDependencyError,
    MissingDependencyError,
    PluginInitError,
)
from app.plugins.registry import PluginRegistry

log = logging.getLogger(__name__)


class ChangeKind(enum.Enum):
    """一次重装中单个插件的变更类别。"""

    STARTED = "started"        # 本轮新起来（新登记或失败后恢复）
    REPLACED = "replaced"      # 同名新实例替换并初始化成功
    RESTARTED = "restarted"    # 因上游被替换/移除而被迫重启
    REMOVED = "removed"        # 被下线并释放
    UNCHANGED = "unchanged"    # 未受影响，保持运行
    FAILED = "failed"          # 装配失败（init 抛错 / 缺依赖 / 依赖成环）
    BLOCKED = "blocked"        # 被失败的上游牵连，不可用


@dataclass(frozen=True)
class PluginChange:
    """单个插件的变更记录（不可变）。"""

    name: str
    kind: ChangeKind
    reason: str | None = None


@dataclass(frozen=True)
class ReloadReport:
    """一整轮重装的变更报告（不可变，可安全对外发布）。"""

    changes: tuple[PluginChange, ...] = ()
    dispose_errors: tuple[tuple[str, str], ...] = ()  # (插件名, 释放错误)

    def by_kind(self, kind: ChangeKind) -> tuple[PluginChange, ...]:
        return tuple(c for c in self.changes if c.kind is kind)

    def summary(self) -> str:
        """人可读的变更摘要。"""
        parts: list[str] = []
        labels = {
            ChangeKind.STARTED: "新启动",
            ChangeKind.REPLACED: "已替换",
            ChangeKind.RESTARTED: "已重启",
            ChangeKind.REMOVED: "已下线",
            ChangeKind.UNCHANGED: "保持不变",
            ChangeKind.FAILED: "装配失败",
            ChangeKind.BLOCKED: "被牵连不可用",
        }
        for kind in ChangeKind:
            group = self.by_kind(kind)
            if not group:
                continue
            items = ", ".join(
                f"{c.name}（{c.reason}）" if c.reason else c.name for c in group
            )
            parts.append(f"{labels[kind]}: {items}")
        if self.dispose_errors:
            errs = ", ".join(f"{n}（{e}）" for n, e in self.dispose_errors)
            parts.append(f"释放报错: {errs}")
        return "；".join(parts) if parts else "无变更"


class PluginReloader:
    """在运行期对注册表与装配器做一整轮原子重装。

    多线程安全：reload() 全程在重载锁内串行执行；状态提交通过
    PluginAssembler.replace_states() 原子完成，并发查询不会看到中间态。
    """

    def __init__(self, registry: PluginRegistry, assembler: PluginAssembler) -> None:
        self._registry = registry
        self._assembler = assembler
        self._lock = threading.RLock()
        self._last_report: ReloadReport | None = None

    # ---------- 对外入口 ----------

    def reload(self, plugins: Iterable[Plugin] | Mapping[str, Plugin]) -> ReloadReport:
        """用给定的插件集合整体替换当前集合并重新装配，返回变更报告。"""
        new_map = self._normalize(plugins)
        with self._lock:
            old_map = self._registry.all()
            old_states = {s.name: s for s in self._assembler.statuses()}
            log.info(
                "重装输入: 当前=%s, 目标=%s",
                sorted(old_map), sorted(new_map),
            )

            removed, replaced, added, kept = self._diff(old_map, new_map)
            impacted = self._impacted_kept(kept, new_map, removed | replaced)
            log.info(
                "差异: 新增=%s, 替换=%s, 移除=%s, 受牵连需重启=%s, 其余保持=%s",
                sorted(added), sorted(replaced), sorted(removed),
                sorted(impacted), sorted(kept - impacted),
            )

            dispose_errors = self._unload_phase(
                old_map, old_states, removed | replaced | impacted
            )
            self._registry.swap(new_map)
            carried_over = {
                n for n in kept - impacted
                if old_states.get(n) and old_states[n].state is PluginState.INITIALIZED
            }
            new_states, init_order = self._assemble_phase(
                new_map, old_states, kept - impacted
            )
            self._assembler.replace_states(new_states, init_order)

            report = self._build_report(
                new_states, removed, replaced, impacted, carried_over, dispose_errors
            )
            self._last_report = report
            log.info("重装完成: %s", report.summary())
            return report

    def last_report(self) -> ReloadReport | None:
        """返回最近一轮重装报告（若有）。"""
        with self._lock:
            return self._last_report

    # ---------- 内部步骤 ----------

    @staticmethod
    def _normalize(plugins: Iterable[Plugin] | Mapping[str, Plugin]) -> dict[str, Plugin]:
        """规整输入为 {名称: 插件}，拒绝空名与同名重复。"""
        items = plugins.values() if isinstance(plugins, Mapping) else plugins
        new_map: dict[str, Plugin] = {}
        for p in items:
            if not p.name:
                raise ValueError("插件名称不能为空")
            if p.name in new_map:
                raise ValueError(f"输入中存在同名插件: {p.name}")
            new_map[p.name] = p
        return new_map

    @staticmethod
    def _diff(
        old_map: dict[str, Plugin], new_map: dict[str, Plugin]
    ) -> tuple[set[str], set[str], set[str], set[str]]:
        """计算差异：移除 / 替换（同名不同实例）/ 新增 / 保留。"""
        old_names, new_names = set(old_map), set(new_map)
        removed = old_names - new_names
        added = new_names - old_names
        common = old_names & new_names
        replaced = {n for n in common if old_map[n] is not new_map[n]}
        kept = common - replaced
        return removed, replaced, added, kept

    @staticmethod
    def _impacted_kept(
        kept: set[str], new_map: dict[str, Plugin], changed: set[str]
    ) -> set[str]:
        """保留插件中，传递依赖到被替换/移除插件的，必须一并重启。"""
        impacted: set[str] = set()

        def hits_changed(name: str, seen: set[str]) -> bool:
            if name in seen:
                return False
            seen.add(name)
            plugin = new_map.get(name)
            if plugin is None:
                return False
            for dep in plugin.dependencies:
                if dep in changed or hits_changed(dep, seen):
                    return True
            return False

        for name in kept:
            if hits_changed(name, set()):
                impacted.add(name)
        return impacted

    def _unload_phase(
        self,
        old_map: dict[str, Plugin],
        old_states: dict[str, PluginStatus],
        targets: set[str],
    ) -> tuple[tuple[str, str], ...]:
        """按旧装配顺序的逆序释放目标插件；单个报错不影响其余，错误被记录。"""
        order = self._topo_order(old_map, tolerate=True)
        errors: list[tuple[str, str]] = []
        log.info("卸载顺序=%s", [n for n in reversed(order) if n in targets])
        for name in reversed(order):
            if name not in targets:
                continue
            status = old_states.get(name)
            if not status or status.state is not PluginState.INITIALIZED:
                log.info("卸载 %s: 之前未初始化（%s），无需释放",
                         name, status.state.value if status else "无状态")
                continue
            try:
                old_map[name].dispose()
            except Exception as exc:  # 隔离：释放报错不卡住其它插件
                errors.append((name, repr(exc)))
                log.warning("卸载 %s: dispose 报错（已记录，继续释放其余）: %r", name, exc)
            else:
                log.info("卸载 %s: 已释放", name)
        return tuple(errors)

    def _assemble_phase(
        self,
        new_map: dict[str, Plugin],
        old_states: dict[str, PluginStatus],
        carried: set[str],
    ) -> tuple[dict[str, PluginStatus], list[str]]:
        """按拓扑序装配新集合；失败隔离并沿依赖链传播为 BLOCKED。"""
        states: dict[str, PluginStatus] = {}

        # 1) 缺失依赖：直接判 FAILED，不参与拓扑排序
        orderable: dict[str, Plugin] = {}
        for name, plugin in new_map.items():
            missing = [d for d in plugin.dependencies if d not in new_map]
            if missing:
                err = MissingDependencyError(name, missing)
                states[name] = PluginStatus(name, PluginState.FAILED, repr(err))
                log.info("装配 %s: 不可用，判定依据: 缺失依赖 %s", name, missing)
            else:
                orderable[name] = plugin

        # 2) 拓扑排序；成环的节点判 FAILED
        order = self._topo_order(orderable, tolerate=True)
        cyclic = set(orderable) - set(order)
        for name in sorted(cyclic):
            err = CyclicDependencyError(sorted(cyclic))
            states[name] = PluginStatus(name, PluginState.FAILED, repr(err))
            log.info("装配 %s: 不可用，判定依据: 依赖成环 %s", name, sorted(cyclic))

        # 3) 依拓扑序初始化；上游失败则阻断下游
        log.info("装配顺序=%s", order)
        init_order: list[str] = []
        blocked_root: dict[str, str] = {}  # 被阻断插件 -> 根因失败插件名
        for name in order:
            plugin = orderable[name]
            old = old_states.get(name)
            if name in carried and old and old.state is PluginState.INITIALIZED:
                states[name] = PluginStatus(name, PluginState.INITIALIZED)
                init_order.append(name)
                log.info("装配 %s: 可用，判定依据: 未受影响且已在运行，保持不重启", name)
                continue
            bad_upstream = [
                d for d in plugin.dependencies
                if states[d].state in (PluginState.FAILED, PluginState.BLOCKED)
            ]
            if bad_upstream:
                up = bad_upstream[0]
                # 直接上游若也是被牵连的，则根因沿链路上溯到真正失败的插件
                root = blocked_root.get(up, up)
                blocked_root[name] = root
                root_cause = states[root].error or states[root].state.value
                reason = f"被上游 {up} 牵连不可用（根因: {root} 装配失败: {root_cause}）"
                states[name] = PluginStatus(name, PluginState.BLOCKED, reason)
                log.info("装配 %s: 不可用，判定依据: %s", name, reason)
                continue
            try:
                plugin.init()
            except Exception as exc:  # 隔离：单个失败不影响其余插件
                err = PluginInitError(name, exc)
                states[name] = PluginStatus(name, PluginState.FAILED, repr(err))
                log.info("装配 %s: 不可用，判定依据: init 抛错 %r", name, exc)
                continue
            states[name] = PluginStatus(name, PluginState.INITIALIZED)
            init_order.append(name)
            log.info("装配 %s: 可用，判定依据: init 成功", name)
        return states, init_order

    @staticmethod
    def _topo_order(plugins: dict[str, Plugin], tolerate: bool) -> list[str]:
        """Kahn 拓扑排序；tolerate=True 时成环节点被略过（由调用方另行标记）。"""
        indegree = {n: 0 for n in plugins}
        dependents: dict[str, list[str]] = {n: [] for n in plugins}
        for name, plugin in plugins.items():
            for dep in plugin.dependencies:
                if dep not in plugins:  # 缺失依赖由调用方先处理
                    continue
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
        return order

    @staticmethod
    def _build_report(
        new_states: dict[str, PluginStatus],
        removed: set[str],
        replaced: set[str],
        impacted: set[str],
        carried_over: set[str],
        dispose_errors: tuple[tuple[str, str], ...],
    ) -> ReloadReport:
        changes: list[PluginChange] = []
        for name in sorted(removed):
            changes.append(PluginChange(name, ChangeKind.REMOVED))
        for name in sorted(new_states):
            status = new_states[name]
            if status.state is PluginState.FAILED:
                changes.append(PluginChange(name, ChangeKind.FAILED, status.error))
            elif status.state is PluginState.BLOCKED:
                changes.append(PluginChange(name, ChangeKind.BLOCKED, status.error))
            elif name in replaced:
                changes.append(PluginChange(name, ChangeKind.REPLACED))
            elif name in impacted:
                changes.append(PluginChange(name, ChangeKind.RESTARTED))
            elif name in carried_over:
                changes.append(PluginChange(name, ChangeKind.UNCHANGED))
            else:
                # 本轮新起来：新登记，或上一轮未运行（失败/被阻断/已释放）本轮恢复
                changes.append(PluginChange(name, ChangeKind.STARTED))
        return ReloadReport(tuple(changes), dispose_errors)
