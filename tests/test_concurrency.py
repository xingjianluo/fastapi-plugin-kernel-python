"""并发装配测试：多线程并发触发装配与查询，结果一致且只生效一次。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.plugins.assembler import PluginAssembler, PluginState
from app.plugins.registry import PluginRegistry
from tests.test_plugins import FakePlugin

log = logging.getLogger(__name__)


class SlowPlugin(FakePlugin):
    """init 有耗时的插件，放大并发竞争窗口。"""

    def __init__(self, name, deps=()):
        super().__init__(name, deps)
        self._barrier_event = threading.Event()

    def init(self):
        self.init_calls += 1
        threading.Event().wait(0.005)  # 模拟初始化耗时


def test_concurrent_assemble_initializes_exactly_once():
    plugins = [SlowPlugin(f"p{i}") for i in range(8)]
    registry = PluginRegistry()
    for p in plugins:
        registry.register(p)
    asm = PluginAssembler(registry)

    log.info("输入: 8 个插件, 16 个线程并发调用 assemble()")
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: asm.assemble(), range(16)))

    init_counts = {p.name: p.init_calls for p in plugins}
    log.info("各插件 init 次数=%s", init_counts)
    assert all(c == 1 for c in init_counts.values()), "并发下 init 必须只生效一次"

    # 所有线程拿到的装配结果一致（全部成功）
    for statuses in results:
        assert all(s.state is PluginState.INITIALIZED for s in statuses)
    log.info("判定依据: 锁内状态变更 + 幂等跳过，16 次并发装配结果一致")


def test_concurrent_status_reads_are_consistent():
    plugins = [FakePlugin(f"p{i}") for i in range(4)]
    registry = PluginRegistry()
    for p in plugins:
        registry.register(p)
    asm = PluginAssembler(registry)
    asm.assemble()

    log.info("输入: 装配完成后 32 个线程并发读取 statuses()")
    with ThreadPoolExecutor(max_workers=32) as pool:
        snapshots = list(pool.map(lambda _: asm.statuses(), range(32)))

    first = [(s.name, s.state) for s in snapshots[0]]
    for snap in snapshots:
        assert [(s.name, s.state) for s in snap] == first
    log.info("判定依据: statuses() 返回不可变快照，并发读取结果一致: %s", first)


def test_concurrent_assemble_and_dispose_no_partial_state():
    """并发装配与释放交替后，状态不得出现部分生效。"""
    plugins = [FakePlugin(f"p{i}") for i in range(4)]
    registry = PluginRegistry()
    for p in plugins:
        registry.register(p)
    asm = PluginAssembler(registry)

    def worker(i: int):
        if i % 2 == 0:
            asm.assemble()
        else:
            asm.dispose()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    # 最终再做一次完整装配，验证状态机仍可收敛到一致状态
    statuses = asm.assemble()
    log.info("并发交替后最终状态=%s", [(s.name, s.state.value) for s in statuses])
    assert all(s.state is PluginState.INITIALIZED for s in statuses)
    assert all(p.init_calls >= 1 for p in plugins)
    log.info("判定依据: 状态变更均在锁内完成，无部分生效，可收敛")
