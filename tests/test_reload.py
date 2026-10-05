"""热重载测试：增/换/删、报告、原子快照、失败传播与恢复、卸载错误隔离。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.plugins.assembler import PluginState
from app.plugins.errors import CyclicDependencyError
from app.plugins.manager import PluginManager
from tests.test_plugins import FakePlugin

log = logging.getLogger(__name__)


class FlakyDisposePlugin(FakePlugin):
    """dispose 会抛错的插件。"""

    def dispose(self):
        self.dispose_calls += 1
        raise RuntimeError(f"{self._name} dispose exploded")


def states_of(manager: PluginManager) -> dict[str, PluginState]:
    return {s.name: s.state for s in manager.statuses()}


def test_reload_add_replace_remove_and_report():
    a, b = FakePlugin("a"), FakePlugin("b", ["a"])
    m = PluginManager()
    m.reload(add=[a, b])

    b2 = FakePlugin("b", ["a"])  # 同名新实例 = 替换
    c = FakePlugin("c")
    log.info("输入: 替换 b（新实例）、新增 c、无移除；上一轮有 a<-b 已初始化")
    report = m.reload(add=[b2, c])
    log.info("报告=%s", report)

    assert report.started == ("c",)       # 全新启动
    assert report.replaced == ("b",)      # 被换掉
    assert report.removed == ()
    assert report.kept == ("a",)          # 未变化继续服役
    assert a.init_calls == 1              # 未变化的不得重复启动
    assert b.dispose_calls == 1 and b2.init_calls == 1  # 旧实例释放、新实例启动
    assert states_of(m) == {
        "a": PluginState.INITIALIZED,
        "b": PluginState.INITIALIZED,
        "c": PluginState.INITIALIZED,
    }

    log.info("输入: 移除 b 和 c")
    report = m.reload(remove=["b", "c"])
    log.info("报告=%s", report)
    assert report.removed == ("b", "c")
    assert states_of(m) == {"a": PluginState.INITIALIZED}
    log.info("判定依据: 报告分类正确，未变化插件不重复 init，移除后快照无残留")


def test_removed_plugin_does_not_come_back():
    a, b = FakePlugin("a"), FakePlugin("b")
    m = PluginManager()
    m.reload(add=[a, b])
    m.reload(remove=["b"])
    log.info("输入: 移除 b 后连续两轮空变更 reload")
    m.reload()
    report = m.reload()
    log.info("报告=%s 快照=%s", report, states_of(m))
    assert "b" not in states_of(m), "已卸载的插件不得被后续轮次带回来"
    assert b.dispose_calls == 1
    assert report.kept == ("a",) and not report.started
    log.info("判定依据: 卸载即彻底清除运行态，反复重装状态不累积")


def test_repeated_reload_is_stable():
    a = FakePlugin("a")
    m = PluginManager()
    m.reload(add=[a])
    first = [(s.name, s.state) for s in m.statuses()]
    for _ in range(5):
        m.reload()
    again = [(s.name, s.state) for s in m.statuses()]
    log.info("首轮=%s 五轮空变更后=%s init_calls=%d", first, again, a.init_calls)
    assert again == first
    assert a.init_calls == 1
    log.info("判定依据: 空变更重装是幂等的，状态不越滚越乱")


def test_dependency_failure_blocks_dependents_and_recovers():
    bad = FakePlugin("bad", fail_on_init=True)
    mid = FakePlugin("mid", ["bad"])
    top = FakePlugin("top", ["mid"])   # 间接依赖
    ok = FakePlugin("ok")
    m = PluginManager()
    log.info("输入: bad.init 抛错, mid 直接依赖 bad, top 间接依赖, ok 独立")
    report = m.reload(add=[bad, mid, top, ok])

    states = states_of(m)
    log.info("快照=%s", {n: s.value for n, s in states.items()})
    assert states["bad"] is PluginState.FAILED
    assert states["mid"] is PluginState.BLOCKED
    assert states["top"] is PluginState.BLOCKED
    assert states["ok"] is PluginState.INITIALIZED
    assert mid.init_calls == 0 and top.init_calls == 0, "被牵连的插件不得启动"

    by_name = {s.name: s for s in m.statuses()}
    assert by_name["mid"].blocked_by == ("bad",)
    assert by_name["top"].blocked_by == ("mid",)
    assert "bad" in (by_name["top"].error or ""), "间接被牵连也要能追溯到失败源头"
    assert [s.name for s in report.blocked] == ["mid", "top"]
    log.info("判定依据: mid 被 bad 直接牵连, top 被 mid 间接牵连, 报告中指明来源")

    log.info("输入: 修复 bad 后重新 reload（无变更）")
    bad.fail_on_init = False
    report = m.reload()
    states = states_of(m)
    log.info("恢复后快照=%s", {n: s.value for n, s in states.items()})
    assert all(s is PluginState.INITIALIZED for s in states.values())
    assert mid.init_calls == 1 and top.init_calls == 1
    assert ok.init_calls == 1, "一直可用的插件不得重复启动"
    log.info("判定依据: 上游修复后受牵连插件跟着恢复，健康插件不重复 init")


def test_missing_dependency_marks_failed_and_blocks_dependent():
    x = FakePlugin("x", ["ghost"])
    y = FakePlugin("y", ["x"])
    m = PluginManager()
    log.info("输入: x 依赖未注册的 ghost, y 依赖 x")
    report = m.reload(add=[x, y])
    states = states_of(m)
    log.info("快照=%s failed=%s", {n: s.value for n, s in states.items()},
             [(s.name, s.error) for s in report.failed])
    assert states["x"] is PluginState.FAILED
    assert "ghost" in (report.failed[0].error or "")
    assert states["y"] is PluginState.BLOCKED
    log.info("判定依据: 缺失依赖隔离为单插件 FAILED，下游被牵连 BLOCKED，不影响整轮")


def test_dispose_error_does_not_block_others_and_is_recorded():
    bad = FlakyDisposePlugin("bad")
    a = FakePlugin("a")
    b = FakePlugin("b")
    m = PluginManager()
    m.reload(add=[bad, a, b])
    log.info("输入: 移除 bad(dispose 抛错) 和 a，保留 b")
    report = m.reload(remove=["bad", "a"])
    log.info("报告=%s", report)

    assert a.dispose_calls == 1, "其它插件不能被 bad 的释放错误卡住"
    assert len(report.dispose_failures) == 1
    assert report.dispose_failures[0].name == "bad"
    assert "dispose exploded" in report.dispose_failures[0].error
    # 错误留痕可查询
    assert m.last_report().dispose_failures == report.dispose_failures

    log.info("输入: 卸载出错后再 reload，验证从干净状态开始")
    m.reload(add=[FakePlugin("c")])
    states = states_of(m)
    log.info("快照=%s", {n: s.value for n, s in states.items()})
    assert "bad" not in states and "a" not in states, "不得留下半释放的残留"
    assert bad.dispose_calls == 1, "已释放过的不得重复释放"
    assert states == {"b": PluginState.INITIALIZED, "c": PluginState.INITIALIZED}
    log.info("判定依据: 释放错误留痕可查，其它插件照常释放，下一轮无残留")


def test_cyclic_change_aborts_reload_and_keeps_old_snapshot():
    a = FakePlugin("a")
    m = PluginManager()
    m.reload(add=[a])
    before = [(s.name, s.state) for s in m.statuses()]

    p, q = FakePlugin("p", ["q"]), FakePlugin("q", ["p"])
    log.info("输入: 新增 p<->q 互相依赖（成环）")
    with pytest.raises(CyclicDependencyError):
        m.reload(add=[p, q])
    after = [(s.name, s.state) for s in m.statuses()]
    log.info("中止后快照=%s", after)
    assert after == before, "本轮中止后读者看到的仍是上一整轮的完整结果"
    assert a.init_calls == 1
    log.info("判定依据: 循环依赖整轮中止，已发布快照不变，无中间态")


def test_concurrent_readers_never_see_partial_round():
    class SlowPlugin(FakePlugin):
        def init(self):
            self.init_calls += 1
            threading.Event().wait(0.01)  # 放大装配窗口

    old = [SlowPlugin(f"old{i}") for i in range(4)]
    m = PluginManager()
    m.reload(add=old)
    old_snapshot = [(s.name, s.state) for s in m.statuses()]

    new = [SlowPlugin(f"new{i}") for i in range(4)]
    stop = threading.Event()
    observations: list[list] = []

    def reader():
        while not stop.is_set():
            observations.append([(s.name, s.state) for s in m.statuses()])

    log.info("输入: 一轮全量替换（4 个慢插件）期间，4 个读者线程持续读快照")
    with ThreadPoolExecutor(max_workers=5) as pool:
        readers = [pool.submit(reader) for _ in range(4)]
        m.reload(add=new, remove=[p.name for p in old])
        stop.set()
        for r in readers:
            r.result()

    new_snapshot = [(s.name, s.state) for s in m.statuses()]
    log.info("旧轮=%s 新轮=%s 观测次数=%d", old_snapshot, new_snapshot, len(observations))
    for obs in observations:
        assert obs in (old_snapshot, new_snapshot), \
            f"读到装配到一半的中间态: {obs}"
    log.info("判定依据: 快照原子切换，任意时刻读到的都是某一整轮的完整结果")
