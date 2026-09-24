"""服务入口：分层配置加载 + 插件装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import build_router
from app.config.loader import ConfigLoader, FieldSpec
from app.config.sources import env_source
from app.plugins.assembler import PluginAssembler
from app.plugins.registry import PluginRegistry

CONFIG_SPECS = [
    FieldSpec("host", str, default="127.0.0.1"),
    FieldSpec("port", int, default=8000),
    FieldSpec("debug", bool, default=False),
]


class DemoPlugin:
    """演示插件：记录初始化次数以验证一次性语义。"""

    def __init__(self, name: str, dependencies: list[str] | None = None) -> None:
        self._name = name
        self._dependencies = dependencies or []
        self.init_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def dependencies(self) -> list[str]:
        return self._dependencies

    def init(self) -> None:
        self.init_count += 1

    def dispose(self) -> None:
        pass


def create_app() -> FastAPI:
    # 1) 分层配置：缺省值 < 环境变量（可继续叠加文件/外部覆盖来源）
    loader = ConfigLoader(CONFIG_SPECS)
    resolved = loader.load([env_source()])

    # 2) 插件装配
    registry = PluginRegistry()
    registry.register(DemoPlugin("core"))
    registry.register(DemoPlugin("metrics", dependencies=["core"]))
    assembler = PluginAssembler(registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        assembler.assemble()
        yield
        assembler.dispose()

    app = FastAPI(lifespan=lifespan)
    app.state.resolved_config = resolved
    app.state.assembler = assembler
    app.include_router(build_router())
    return app


app = create_app()
