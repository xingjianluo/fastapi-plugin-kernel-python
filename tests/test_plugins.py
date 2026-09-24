"""插件装配测试：依赖解析、错误分类、失败隔离与生命周期一次性。"""
from __future__ import annotations

import logging

import pytest

from app.plugins.assembler import PluginAssembler, PluginState
from app.plugins.errors import (
    CyclicDependencyError,
    DuplicateRegistrationError,
    MissingDependencyError,
)
from app.plugins.registry import PluginRegistry

log = logging.getLogger(__name__)


class FakePlugin:
    """记录生命周期调用次数的测试插件。"""

    def __init__(self, name, deps=(), fail_on_init=False):
        self._name = name
        self._deps = list(deps)
        self.fail_on_init = fail_on_init
        self.init_calls = 0
        self.dispose_calls = 0

    @property
    def name(self):
        return self._name

    @property
    def dependencies(self):
        return self._deps

    def init(self):
        self.init_calls += 1
        if self.fail_on_init:
            raise RuntimeError(f"{self._name} init exploded")

    def dispose(self):
        self.dispose_calls += 1


def make_assembler(*plugins) -> PluginAssembler:
    registry = PluginRegistry()
    for p in plugins:
        registry.register(p)
    return PluginAssembler(registry)


def test_topological_order():
    a, b, c = FakePlugin("a"), FakePlugin("b", ["a"]), FakePlugin("c", ["b", "a"])
    asm = make_assembler(c, a, b)  # 注册顺序打乱
    order = asm.resolve_order()
    log.info("输入: a<-b<-c, c 还依赖 a；注册顺序=[c,a,b]")
    log.info("装配顺序=%s，判定依据: 依赖必须先于依赖者出现", order)
    assert order.index("a") < order.index("b") < order.index("c")


def test_missing_dependency_rejected():
    asm = make_assembler(FakePlugin("x", ["ghost"]))
    log.info("输入: 插件 x 依赖未注册的 ghost")
    with pytest.raises(MissingDependencyError) as exc:
        asm.resolve_order()
    log.info("判定依据: 依赖不在注册表 -> MissingDependencyError: %s", exc.value)
    assert exc.value.missing == ["ghost"]


def test_cyclic_dependency_rejected():
    asm = make_assembler(FakePlugin("p", ["q"]), FakePlugin("q", ["p"]))
    log.info("输入: p<->q 互相依赖")
    with pytest.raises(CyclicDependencyError) as exc:
        asm.resolve_order()
    log.info("判定依据: 拓扑排序后仍有剩余节点，DFS 找到环: %s", exc.value.cycle)
    assert set(exc.value.cycle) >= {"p", "q"}


def test_duplicate_registration_rejected():
    registry = PluginRegistry()
    registry.register(FakePlugin("dup"))
    log.info("输入: 同名插件 dup 注册两次")
    with pytest.raises(DuplicateRegistrationError) as exc:
        registry.register(FakePlugin("dup"))
    log.info("判定依据: 名称已存在 -> DuplicateRegistrationError: %s", exc.value)


def test_single_failure_isolated_and_queryable():
    ok, bad = FakePlugin("ok"), FakePlugin("bad", fail_on_init=True)
    asm = make_assembler(ok, bad)
    statuses = asm.assemble()
    log.info("输入: ok 正常, bad.init 抛 RuntimeError")
    log.info("装配结果=%s", [(s.name, s.state.value, s.error) for s in statuses])
    by_name = {s.name: s for s in statuses}
    assert by_name["ok"].state is PluginState.INITIALIZED
    assert by_name["bad"].state is PluginState.FAILED
    # 失败状态可被外部查询且带原始错误信息
    status = asm.status("bad")
    assert status is not None and "RuntimeError" in (status.error or "")
    assert "init exploded" in (status.error or "")
    log.info("判定依据: bad 被隔离为 FAILED 且保留原始错误，ok 不受影响")


def test_retry_after_failure_is_consistent():
    bad = FakePlugin("bad", fail_on_init=True)
    ok = FakePlugin("ok")
    asm = make_assembler(ok, bad)
    asm.assemble()
    bad.fail_on_init = False
    statuses = asm.assemble()  # 重试
    log.info("重试后状态=%s, ok.init_calls=%d bad.init_calls=%d",
             [(s.name, s.state.value) for s in statuses], ok.init_calls, bad.init_calls)
    assert all(s.state is PluginState.INITIALIZED for s in statuses)
    assert ok.init_calls == 1   # 已成功的不得重复初始化
    assert bad.init_calls == 2  # 失败的允许重试一次


def test_init_and_dispose_happen_exactly_once():
    a, b = FakePlugin("a"), FakePlugin("b", ["a"])
    asm = make_assembler(a, b)
    asm.assemble()
    asm.assemble()  # 重复触发装配
    assert a.init_calls == 1 and b.init_calls == 1
    asm.dispose()
    asm.dispose()  # 重复触发释放
    assert a.dispose_calls == 1 and b.dispose_calls == 1
    log.info("判定依据: 重复 assemble/dispose 后 init/dispose 各只生效一次")


def test_dispose_reverse_order():
    calls: list[str] = []

    class Ordered(FakePlugin):
        def dispose(self):
            calls.append(self._name)

    a, b = Ordered("a"), Ordered("b", ["a"])
    asm = make_assembler(a, b)
    asm.assemble()
    asm.dispose()
    log.info("装配顺序=[a,b]，释放顺序=%s，判定依据: 逆序释放", calls)
    assert calls == ["b", "a"]
