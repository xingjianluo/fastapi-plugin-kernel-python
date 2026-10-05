"""运行期热重载测试：增删换、失败传播、释放隔离、快照一致性与 API 兼容。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.plugins.assembler import PluginAssembler, PluginState
from app.plugins.registry import PluginRegistry
from app.plugins.reloader import ChangeKind, PluginReloader

log = logging.getLogger(__name__)


class RPlugin:
    """记录生命周期调用次数、可注入失败的热重载测试插件。"""

    def __init__(self, name, deps=(), fail_init=False, fail_dispose=False):
        self._name = name
        self._deps = list(deps)
        self.fail_init = fail_init
        self.fail_dispose = fail_dispose
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
        if self.fail_init:
            raise RuntimeError(f"{self._name} init exploded")

    def dispose(self):
        self.dispose_calls += 1
        if self.fail_dispose:
            raise RuntimeError(f"{self._name} dispose exploded")


def make_reloader() -> PluginReloader:
    return PluginReloader(PluginRegistry(), PluginAssembler(PluginRegistry()))


def states_by_name(asm: PluginAssembler):
    return {s.name: s for s in asm.statuses()}


def test_reload_add_replace_remove_and_report():
    a = RPlugin("a")
    b1 = RPlugin("b", ["a"])
    x = RPlugin("x")
    rel = make_reloader()
    rel.reload([a, b1, x])
    log.info("第 1 轮输入=[a, b(a), x]，全部新启动")

    b2 = RPlugin("b", ["a"])  # 同名新实例
    c = RPlugin("c")
    report = rel.reload([a, b2, c])
    log.info("第 2 轮输入=[a(同实例), b(新实例), c]，x 下线")
    log.info("报告: %s", report.summary())

    kinds = {ch.name: ch.kind for ch in report.changes}
    assert kinds["a"] is ChangeKind.UNCHANGED   # 未受影响
    assert kinds["b"] is ChangeKind.REPLACED    # 同名新实例
    assert kinds["c"] is ChangeKind.STARTED     # 新登记
    assert kinds["x"] is ChangeKind.REMOVED     # 被下线
    log.info("判定依据: 变更分类=%s", {k: v.value for k, v in kinds.items()})

    # 未受影响的已运行插件不得重复启动；被替换/移除的旧实例各释放一次
    assert a.init_calls == 1 and a.dispose_calls == 0
    assert b1.dispose_calls == 1 and b2.init_calls == 1
    assert x.dispose_calls == 1


def test_removed_plugin_state_purged_and_not_revived():
    a, x = RPlugin("a"), RPlugin("x")
    rel = make_reloader()
    rel.reload([a, x])
    rel.reload([a])  # x 下线
    asm = rel._assembler
    log.info("x 下线后状态=%s", sorted(states_by_name(asm)))
    assert asm.status("x") is None, "被移除插件的状态必须彻底清除"

    report = rel.reload([a])  # 再重装一轮同样的集合
    assert asm.status("x") is None, "已下线插件不得被后续轮次带回来"
    assert all(ch.kind is ChangeKind.UNCHANGED for ch in report.changes)
    assert a.init_calls == 1, "反复重装不得重复启动未受影响插件"
    log.info("判定依据: 状态表每轮整体重建，无残留、无复活")


def test_failure_propagates_to_transitive_dependents_and_recovers():
    base = RPlugin("base", fail_init=True)
    mid = RPlugin("mid", ["base"])
    leaf = RPlugin("leaf", ["mid"])
    solo = RPlugin("solo")
    rel = make_reloader()
    report = rel.reload([base, mid, leaf, solo])
    log.info("输入: base(init 必败) <- mid <- leaf, solo 独立")

    kinds = {ch.name: ch.kind for ch in report.changes}
    assert kinds["base"] is ChangeKind.FAILED
    assert kinds["mid"] is ChangeKind.BLOCKED
    assert kinds["leaf"] is ChangeKind.BLOCKED
    assert kinds["solo"] is ChangeKind.STARTED
    # 被牵连者不得尝试初始化
    assert mid.init_calls == 0 and leaf.init_calls == 0

    asm = rel._assembler
    st = states_by_name(asm)
    assert st["mid"].state is PluginState.BLOCKED
    assert "base" in (st["mid"].error or ""), "报告/查询必须指明被哪个失败上游牵连"
    assert "mid" in (st["leaf"].error or ""), "间接牵连需指明直接上游"
    assert "base" in (st["leaf"].error or ""), "间接牵连需能追溯到根因插件"
    log.info("mid 不可用依据: %s", st["mid"].error)
    log.info("leaf 不可用依据: %s", st["leaf"].error)

    # 修好上游后重新重装，受牵连者跟着恢复
    base.fail_init = False
    report2 = rel.reload([base, mid, leaf, solo])
    kinds2 = {ch.name: ch.kind for ch in report2.changes}
    st2 = states_by_name(asm)
    assert all(s.state is PluginState.INITIALIZED for s in st2.values())
    assert kinds2["mid"] is ChangeKind.STARTED and kinds2["leaf"] is ChangeKind.STARTED
    assert kinds2["solo"] is ChangeKind.UNCHANGED and solo.init_calls == 1
    log.info("修复后报告: %s", report2.summary())


def test_replace_upstream_restarts_transitive_dependents():
    a1 = RPlugin("a")
    b = RPlugin("b", ["a"])
    c = RPlugin("c", ["b"])
    rel = make_reloader()
    rel.reload([a1, b, c])

    a2 = RPlugin("a")  # 替换最上游
    report = rel.reload([a2, b, c])
    kinds = {ch.name: ch.kind for ch in report.changes}
    log.info("替换上游 a 后报告: %s", report.summary())
    assert kinds["a"] is ChangeKind.REPLACED
    assert kinds["b"] is ChangeKind.RESTARTED
    assert kinds["c"] is ChangeKind.RESTARTED
    # 依赖实例变了，下游必须重启以绑定新实例
    assert a1.dispose_calls == 1 and a2.init_calls == 1
    assert b.init_calls == 2 and c.init_calls == 2
    log.info("判定依据: 传递依赖被替换上游的保留插件全部重启且各只多初始化一次")


def test_dispose_error_isolated_recorded_and_next_round_clean():
    bad = RPlugin("bad", fail_dispose=True)
    ok1, ok2 = RPlugin("ok1"), RPlugin("ok2", ["ok1"])
    rel = make_reloader()
    rel.reload([bad, ok1, ok2])

    report = rel.reload([])  # 整轮下线
    log.info("下线报告: %s", report.summary())
    assert report.dispose_errors and report.dispose_errors[0][0] == "bad"
    assert "dispose exploded" in report.dispose_errors[0][1]
    # 释放报错不卡住其它插件：该释放的都释放掉
    assert bad.dispose_calls == 1 and ok1.dispose_calls == 1 and ok2.dispose_calls == 1
    assert rel._assembler.statuses() == [], "整轮卸载后不得有状态残留"

    # 卸载后再重装：从干净状态开始，无半释放残留
    bad2, ok1b, ok2b = RPlugin("bad"), RPlugin("ok1"), RPlugin("ok2", ["ok1"])
    report2 = rel.reload([bad2, ok1b, ok2b])
    st = states_by_name(rel._assembler)
    assert all(s.state is PluginState.INITIALIZED for s in st.values())
    assert all(ch.kind is ChangeKind.STARTED for ch in report2.changes)
    log.info("判定依据: 释放错误被记录可查，下一轮从干净状态装配: %s", report2.summary())


def test_missing_dep_and_cycle_are_per_plugin_failures_not_round_failure():
    orphan = RPlugin("orphan", ["ghost"])
    p = RPlugin("p", ["q"])
    q = RPlugin("q", ["p"])
    down = RPlugin("down", ["orphan"])
    fine = RPlugin("fine")
    rel = make_reloader()
    report = rel.reload([orphan, p, q, down, fine])
    log.info("输入: orphan 缺依赖 ghost, p<->q 成环, down 依赖 orphan, fine 正常")
    kinds = {ch.name: ch.kind for ch in report.changes}
    assert kinds["orphan"] is ChangeKind.FAILED
    assert kinds["p"] is ChangeKind.FAILED and kinds["q"] is ChangeKind.FAILED
    assert kinds["down"] is ChangeKind.BLOCKED
    assert kinds["fine"] is ChangeKind.STARTED
    log.info("判定依据: 缺依赖/成环只隔离为单插件失败，整轮装配不中断: %s", report.summary())


def test_repeated_reloads_converge():
    a = RPlugin("a")
    b = RPlugin("b", ["a"])
    rel = make_reloader()
    rel.reload([a, b])
    for i in range(5):
        report = rel.reload([a, b])
    st = states_by_name(rel._assembler)
    log.info("5 轮重复重装后状态=%s", {n: s.state.value for n, s in st.items()})
    assert all(s.state is PluginState.INITIALIZED for s in st.values())
    assert a.init_calls == 1 and b.init_calls == 1
    assert all(ch.kind is ChangeKind.UNCHANGED for ch in report.changes)
    log.info("判定依据: 重复重装幂等收敛，状态不越滚越乱")


def test_concurrent_reload_and_reads_see_whole_rounds():
    a = RPlugin("a")
    set_a = [a, RPlugin("b", ["a"])]
    set_b = [a, RPlugin("c", ["a"])]
    rel = make_reloader()
    rel.reload(set_a)
    asm = rel._assembler
    names_a, names_b = {"a", "b"}, {"a", "c"}

    stop = threading.Event()
    violations: list[tuple] = []

    def reader():
        while not stop.is_set():
            names = frozenset(s.name for s in asm.statuses())
            if names not in (names_a, names_b):
                violations.append(tuple(sorted(names)))

    def reloader(i):
        rel.reload(set_a if i % 2 == 0 else set_b)

    log.info("输入: 8 线程交替重装 {a,b}/{a,c}，4 线程并发读状态快照")
    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(reloader, range(16)))
    stop.set()
    for t in readers:
        t.join()

    assert not violations, f"读到装配到一半的中间态: {violations[:3]}"
    assert a.init_calls == 1, "共享的未受影响插件不得被重复启动"
    assert a.dispose_calls == 0
    log.info("判定依据: 状态表原子切换，并发读取只见整轮结果；a.init_calls=%d", a.init_calls)


def test_api_compatibility_and_reload_endpoints():
    from fastapi.testclient import TestClient

    from main import create_app

    with TestClient(create_app()) as client:
        # 老查询方式照常可用
        plugins = client.get("/plugins").json()["plugins"]
        log.info("老接口 /plugins 返回=%s", plugins)
        assert {p["name"] for p in plugins} == {"core", "metrics"}
        assert all(p["state"] == "initialized" for p in plugins)

        one = client.get("/plugins/core").json()
        assert one == {"name": "core", "state": "initialized", "error": None}
        unknown = client.get("/plugins/ghost").json()
        assert unknown["state"] == "unknown"

        # 新增的热重载能力
        report = client.post("/reload").json()
        log.info("POST /reload 报告: %s", report["summary"])
        assert report["summary"]
        last = client.get("/reload/report").json()
        assert last["reloaded"] is True
        assert last["summary"] == report["summary"]
        # 重装后老接口行为不变
        plugins2 = client.get("/plugins").json()["plugins"]
        assert {p["name"] for p in plugins2} == {"core", "metrics"}
        log.info("判定依据: 老查询接口返回结构不变，新接口只增不改")
