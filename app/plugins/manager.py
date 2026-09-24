"""插件管理器：注册校验、拓扑排序、并发安全的生命周期编排。"""

from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType
from typing import Any

from app.plugins.contract import Plugin, PluginContext, PluginSpec
from app.plugins.errors import (
    CircularDependencyError,
    DuplicatePluginError,
    MissingDependencyError,
    PluginError,
)
from app.plugins.registry import AssemblyResult, FailureRecord

log = logging.getLogger("plugins.manager")


class _Context(PluginContext):
    """初始化期上下文：只能取到已成功初始化的依赖。"""

    def __init__(self, instances: dict[str, Plugin], current: str) -> None:
        self._instances = instances
        self._current = current

    def require(self, name: str) -> Any:
        if name not in self._instances:
            raise PluginError(
                f"插件 {self._current!r} 请求的依赖 {name!r} 未就绪（缺失或初始化失败）"
            )
        return self._instances[name]


class PluginManager:
    """注册/装配/查询/卸载；单把锁保护状态，装配结果为不可变快照。

    语义：
    - register 立即拒绝重复注册；
    - assemble 前统一校验依赖缺失与循环依赖（注册顺序无关）；
    - 单个插件实例化或 initialize 失败被隔离为 FAILED，其下游标记 SKIPPED；
    - 并发 assemble 共享同一次装配，结果只计算一次且不可变；
    - 失败后可重新 register 修复并重试 assemble，状态整体重建，无部分生效；
    - teardown 按初始化逆序，仅释放成功且未释放者，各只一次；
      initialize 成功但 teardown 抛错会被记录并继续释放其余插件。
    """

    def __init__(self) -> None:
        self._specs: dict[str, PluginSpec] = {}
        self._instances: dict[str, Plugin] = {}
        self._result: AssemblyResult | None = None
        self._teardown_errors: list[FailureRecord] = []
        self._assemble_task: asyncio.Task[AssemblyResult] | None = None
        self._teardown_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ---------- 注册 ----------

    def register(self, spec: PluginSpec) -> None:
        if spec.name in self._specs:
            raise DuplicatePluginError(f"插件重复注册: name={spec.name!r}")
        self._specs[spec.name] = spec
        # 新声明使旧装配结果失效，强制重试时整体重建，避免部分生效。
        self._instances = {}
        self._result = None
        self._assemble_task = None
        self._teardown_task = None
        log.info("注册插件 name=%s deps=%s", spec.name, list(spec.dependencies))

    # ---------- 依赖图校验 / 拓扑排序 ----------

    def assembly_order(self) -> list[str]:
        """Kahn 拓扑排序；先报缺失依赖，再报循环依赖（原因可区分）。"""
        # 1) 缺失依赖
        missing: list[tuple[str, list[str]]] = []
        for name, spec in self._specs.items():
            absent = [d for d in spec.dependencies if d not in self._specs]
            if absent:
                missing.append((name, absent))
        if missing:
            name, absent = missing[0]
            raise MissingDependencyError(plugin=name, missing=absent)

        # 2) 入度（按注册顺序处理，保证同层顺序确定）
        names = list(self._specs)
        indeg = {n: 0 for n in names}
        dependents: dict[str, list[str]] = {n: [] for n in names}
        for name in names:
            for dep in self._specs[name].dependencies:
                indeg[name] += 1
                dependents[dep].append(name)

        ready = [n for n in names if indeg[n] == 0]
        order: list[str] = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for child in dependents[n]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    ready.append(child)
            ready.sort(key=names.index)

        if len(order) != len(names):
            unresolved = [n for n in names if n not in order]
            raise CircularDependencyError(cycle=self._find_cycle(unresolved))
        return order

    def _find_cycle(self, unresolved: list[str]) -> list[str]:
        """在未决节点子图中用 DFS 找一条具体的环。"""
        white, gray, black = 0, 1, 2
        color = {n: white for n in unresolved}
        stack: list[str] = []

        def dfs(node: str) -> list[str] | None:
            color[node] = gray
            stack.append(node)
            for dep in self._specs[node].dependencies:
                if dep not in color:
                    continue
                if color[dep] == gray:
                    i = stack.index(dep)
                    return stack[i:] + [dep]
                if color[dep] == white:
                    found = dfs(dep)
                    if found:
                        return found
            stack.pop()
            color[node] = black
            return None

        for n in unresolved:
            if color[n] == white:
                found = dfs(n)
                if found:
                    return found
        return unresolved

    # ---------- 装配 ----------

    async def assemble(self) -> AssemblyResult:
        """执行（或共享）一次装配，返回不可变快照。"""
        async with self._lock:
            if self._result is not None:
                return self._result
            if self._assemble_task is None:
                self._assemble_task = asyncio.ensure_future(self._assemble_locked())
            task = self._assemble_task
        try:
            return await task
        finally:
            # 失败后清除任务，允许后续重试；结果引用由 _assemble_locked 管理。
            async with self._lock:
                if self._assemble_task is task and self._result is None:
                    self._assemble_task = None

    async def _assemble_locked(self) -> AssemblyResult:
        # 拓扑校验：依赖缺失 / 循环依赖直接拒绝，不产生任何实例。
        order = self.assembly_order()
        log.info("判定装配顺序=%s（依据：依赖先于被依赖者，Kahn 拓扑排序）", order)

        # 先统一实例化（捕获工厂同步异常），不做部分初始化。
        instances: dict[str, Plugin] = {}
        instantiated: set[str] = set()
        failures: dict[str, FailureRecord] = {}
        for name in order:
            try:
                instances[name] = self._specs[name].factory()
                instantiated.add(name)
            except Exception as exc:  # noqa: BLE001 - 需保留原始错误并隔离
                failures[name] = FailureRecord(
                    plugin=name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    original_error=exc,
                )
                log.error("插件实例化失败 name=%s error=%r", name, exc)

        initialized: list[str] = []
        skipped: set[str] = set()

        for name in order:
            if name in failures:
                continue
            if any(
                d not in instances
                or d in failures
                or d in skipped
                for d in self._specs[name].dependencies
            ):
                skipped.add(name)
                log.warning("插件因依赖未就绪被跳过 name=%s", name)
                continue
            try:
                log.info("初始化插件 name=%s 依赖=%s",
                         name, list(self._specs[name].dependencies))
                await instances[name].initialize(_Context(instances, name))
                initialized.append(name)
            except Exception as exc:  # noqa: BLE001 - 隔离单插件失败
                failures[name] = FailureRecord(
                    plugin=name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    original_error=exc,
                )
                log.error("插件初始化失败 name=%s error=%r", name, exc)

        # 未成功初始化的实例不进入结果，也不持有引用。
        live = {n: instances[n] for n in initialized}
        result = AssemblyResult(
            order=tuple(order),
            initialized=tuple(initialized),
            failed=tuple(failures[n] for n in order if n in failures),
            skipped=tuple(n for n in order if n in skipped),
            instances=MappingProxyType(dict(live)),
        )
        self._instances = live
        self._result = result
        log.info(
            "装配完成 成功=%s 失败=%s 跳过=%s",
            list(initialized),
            [r.plugin for r in result.failed],
            list(skipped),
        )
        return result

    # ---------- 卸载 ----------

    async def teardown(self) -> None:
        """按初始化逆序释放；幂等；并发共享同一次卸载。"""
        async with self._lock:
            if self._result is None:
                return
            if self._teardown_task is None:
                self._teardown_task = asyncio.ensure_future(self._teardown_locked())
            task = self._teardown_task
        await task

    async def _teardown_locked(self) -> None:
        assert self._result is not None
        order = list(self._result.initialized)
        for name in reversed(order):
            inst = self._instances.get(name)
            if inst is None:
                continue
            try:
                log.info("释放插件 name=%s", name)
                await inst.teardown()
            except Exception as exc:  # noqa: BLE001 - 记录后继续释放其余插件
                rec = FailureRecord(
                    plugin=name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    original_error=exc,
                )
                self._teardown_errors.append(rec)
                log.error("插件释放失败 name=%s error=%r（继续释放其余插件）", name, exc)
            finally:
                self._instances.pop(name, None)

        self._result = AssemblyResult(
            order=self._result.order,
            initialized=(),
            failed=self._result.failed,
            skipped=self._result.skipped,
            instances=MappingProxyType({}),
        )
        self._instances = {}
        log.info("卸载完成，释放顺序（逆序）=%s", list(reversed(order)))

    # ---------- 查询 ----------

    def result(self) -> AssemblyResult:
        if self._result is None:
            raise PluginError("尚未完成装配，无装配结果可查询")
        return self._result

    def get(self, name: str) -> Plugin:
        if self._result is None:
            raise PluginError(f"插件 {name!r} 不可用：尚未装配")
        if not self._result.is_healthy(name):
            rec = self._result.failure_of(name)
            detail = f"，原始错误={rec.error_type}: {rec.message}" if rec else "（未成功初始化）"
            raise PluginError(f"插件 {name!r} 不可用{detail}")
        return self._instances[name]

    @property
    def teardown_errors(self) -> tuple[FailureRecord, ...]:
        return tuple(self._teardown_errors)


__all__ = ["PluginManager"]
