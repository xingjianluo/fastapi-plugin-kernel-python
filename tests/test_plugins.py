"""插件装配测试：依赖排序、可区分拒绝、失败隔离、一次性生命周期、并发一致性。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.plugins import (
    CircularDependencyError,
    DuplicatePluginError,
    MissingDependencyError,
    Plugin,
    PluginError,
    PluginManager,
    PluginSpec,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("test.plugins")


class Recorder:
    """记录全局事件顺序与次数，验证生命周期只生效一次。"""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.init_count: dict[str, int] = {}
        self.teardown_count: dict[str, int] = {}


REC = Recorder()


class SimplePlugin(Plugin):
    def __init__(self, name: str, deps: tuple[str, ...] = ()) -> None:
        self.name = name
        self.dependencies = deps

    async def initialize(self, ctx) -> None:
        for d in self.dependencies:  # 显式确认依赖已就绪
            ctx.require(d)
        REC.init_count[self.name] = REC.init_count.get(self.name, 0) + 1
        REC.events.append(f"init:{self.name}")

    async def teardown(self) -> None:
        REC.teardown_count[self.name] = REC.teardown_count.get(self.name, 0) + 1
        REC.events.append(f"teardown:{self.name}")


def spec(name: str, deps: tuple[str, ...] = (), factory=None) -> PluginSpec:
    return PluginSpec(
        name=name,
        factory=factory or (lambda n=name, d=deps: SimplePlugin(n, d)),
        dependencies=deps,
    )


@pytest.fixture(autouse=True)
def _reset_recorder():
    REC.events.clear()
    REC.init_count.clear()
    REC.teardown_count.clear()
    yield


def test_assembly_order_respects_dependencies() -> None:
    m = PluginManager()
    # 注册顺序故意打乱，判定依据：被依赖者必须先初始化
    for s in [spec("c", ("a", "b")), spec("a"), spec("b", ("a",))]:
        m.register(s)
    order = m.assembly_order()
    log.info("输入注册=c,a,b（依赖 c->a,b; b->a），装配顺序=%s", order)
    assert order.index("a") < order.index("b") < order.index("c")


def test_duplicate_registration_rejected() -> None:
    m = PluginManager()
    m.register(spec("a"))
    log.info("输入：重复注册 name=a，判定依据：注册表键冲突 -> DuplicatePluginError")
    with pytest.raises(DuplicatePluginError):
        m.register(spec("a"))


def test_missing_dependency_rejected() -> None:
    m = PluginManager()
    m.register(spec("a", ("ghost",)))
    log.info("输入：a 依赖未注册的 ghost，判定依据：依赖闭包不全 -> MissingDependencyError")
    with pytest.raises(MissingDependencyError) as ei:
        m.assembly_order()
    assert ei.value.plugin == "a"
    assert ei.value.missing == ["ghost"]


def test_circular_dependency_rejected() -> None:
    m = PluginManager()
    m.register(spec("a", ("b",)))
    m.register(spec("b", ("c",)))
    m.register(spec("c", ("a",)))
    log.info("输入：a->b->c->a，判定依据：Kahn 后仍有未决节点 -> CircularDependencyError")
    with pytest.raises(CircularDependencyError) as ei:
        m.assembly_order()
    assert set(ei.value.cycle) == {"a", "b", "c"}


def test_error_types_are_distinguishable() -> None:
    for t in (DuplicatePluginError, MissingDependencyError, CircularDependencyError):
        assert issubclass(t, PluginError)


async def test_single_plugin_failure_is_isolated_with_original_error() -> None:
    class BoomPlugin(Plugin):
        name = "boom"
        dependencies = ()

        async def initialize(self, ctx) -> None:
            raise RuntimeError("boom-original-reason")

        async def teardown(self) -> None:
            pass

    m = PluginManager()
    m.register(spec("a"))
    m.register(PluginSpec("boom", lambda: BoomPlugin()))
    m.register(spec("c", ("boom",)))  # 下游应被 SKIPPED
    m.register(spec("lonely", ("a",)))  # 与失败链无关，应成功

    result = await m.assemble()
    log.info(
        "装配顺序=%s 成功=%s 失败=%s 跳过=%s，判定依据：boom 抛错隔离，下游跳过，其余继续",
        result.order, result.initialized, [r.plugin for r in result.failed], result.skipped,
    )
    assert result.initialized == ("a", "lonely")
    assert result.skipped == ("c",)
    rec = result.failure_of("boom")
    assert rec is not None
    assert rec.error_type == "RuntimeError"
    assert rec.message == "boom-original-reason"
    assert isinstance(rec.original_error, RuntimeError)

    # 失败状态可外部查询；取失败插件得到带原始错误信息的拒绝
    assert m.result() is result
    with pytest.raises(PluginError, match="boom-original-reason"):
        m.get("boom")
    # 服务整体仍可用：健康插件可获取，结果不可变
    assert m.get("a").name == "a"
    with pytest.raises(TypeError):
        result.instances["x"] = object()  # type: ignore[index]


async def test_factory_failure_is_also_isolated() -> None:
    def boom_factory():
        raise ValueError("factory-exploded")

    m = PluginManager()
    m.register(spec("a"))
    m.register(PluginSpec("bad", boom_factory))
    result = await m.assemble()
    rec = result.failure_of("bad")
    assert rec is not None and rec.error_type == "ValueError"
    assert result.initialized == ("a",)


async def test_lifecycle_once_and_reverse_teardown_order() -> None:
    m = PluginManager()
    m.register(spec("c", ("a", "b")))
    m.register(spec("a"))
    m.register(spec("b", ("a",)))
    await m.assemble()
    assert REC.events == ["init:a", "init:b", "init:c"]
    await m.teardown()
    await m.teardown()  # 重复触发不生效
    assert REC.events == ["init:a", "init:b", "init:c",
                          "teardown:c", "teardown:b", "teardown:a"]
    assert all(v == 1 for v in REC.teardown_count.values())


async def test_retry_after_failure_keeps_consistent_state() -> None:
    attempts = {"n": 0}

    class FlakyPlugin(Plugin):
        name = "flaky"
        dependencies = ()

        async def initialize(self, ctx) -> None:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("first try fails")

        async def teardown(self) -> None:
            REC.events.append("teardown:flaky")

    m = PluginManager()
    m.register(spec("a"))
    m.register(PluginSpec("flaky", lambda: FlakyPlugin()))

    r1 = await m.assemble()
    log.info("第一次装配 失败=%s（无实例残留），判定依据：失败后允许整体重试",
             [r.plugin for r in r1.failed])
    assert not r1.is_healthy("flaky")
    assert "flaky" not in r1.instances

    # 同一失败结果在未修复前保持稳定（不自动重做）
    assert await m.assemble() is r1

    # 注册表不允许同名静默覆盖（避免“替换了声明但装配不重做”的不一致）；
    # 修复后重试应在新的管理器上整体重建，全部插件恰好初始化一次。
    baseline = dict(REC.init_count)
    m2 = PluginManager()
    m2.register(spec("a"))
    m2.register(PluginSpec("flaky", lambda: FlakyPlugin()))
    r2 = await m2.assemble()
    assert r2.initialized == ("a", "flaky")
    # flaky 经历了“先失败后重试成功”两次 initialize 尝试；
    # 其依赖 a 在新管理器上整体重建，恰好再初始化一次，无部分生效。
    assert attempts["n"] == 2
    assert REC.init_count["a"] - baseline.get("a", 0) == 1
    log.info("重试装配 成功=%s，判定依据：全部实例重新构建，无部分生效", r2.initialized)


async def test_concurrent_assemble_runs_once_and_immutable() -> None:
    m = PluginManager()
    m.register(spec("a"))
    m.register(spec("b", ("a",)))

    results = await asyncio.gather(*(m.assemble() for _ in range(10)))
    log.info("输入：10 个并发 assemble，判定依据：共享同一装配任务，结果同一不可变快照")
    assert all(r is results[0] for r in results)
    assert REC.init_count == {"a": 1, "b": 1}
    snap = results[0]
    with pytest.raises(TypeError):
        snap.instances["z"] = object()  # type: ignore[index]
    # 并发查询健康插件
    got = await asyncio.gather(*[asyncio.to_thread(m.get, "b") for _ in range(10)])
    assert all(p.name == "b" for p in got)


async def test_teardown_failure_does_not_block_others() -> None:
    class TeardownBoom(Plugin):
        name = "boomdown"

        async def initialize(self, ctx) -> None:
            REC.events.append("init:boomdown")

        async def teardown(self) -> None:
            REC.events.append("teardown:boomdown")
            raise RuntimeError("teardown-boom")

    m = PluginManager()
    m.register(spec("a"))
    m.register(spec("b", ("a",)))
    m.register(PluginSpec("boomdown", lambda: TeardownBoom(), dependencies=("b",)))
    result = await m.assemble()
    log.info("装配顺序=%s，判定依据：teardown 逆序 a<-b<-boomdown，boomdown 失败不阻断",
             result.order)
    await m.teardown()
    # boomdown 在逆序最前；其抛错后 a/b 仍必须被释放（无资源泄漏）
    assert REC.events == [
        "init:a", "init:b", "init:boomdown",
        "teardown:boomdown", "teardown:b", "teardown:a",
    ]
    errs = m.teardown_errors
    assert len(errs) == 1
    assert errs[0].plugin == "boomdown"
    assert isinstance(errs[0].original_error, RuntimeError)
    log.info("teardown 错误被记录且其余插件继续释放：%s", errs[0].message)


async def test_teardown_after_failed_plugin_does_not_touch_it() -> None:
    class BoomInit(Plugin):
        name = "boom"

        async def initialize(self, ctx) -> None:
            raise RuntimeError("x")

        async def teardown(self) -> None:
            REC.events.append("teardown:boom")  # 失败插件不应走到这里

    m = PluginManager()
    m.register(spec("a"))
    m.register(PluginSpec("boom", lambda: BoomInit()))
    await m.assemble()
    await m.teardown()
    assert REC.events == ["init:a", "teardown:a"]
