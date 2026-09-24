"""FastAPI 装配层：暴露配置查询、插件装配结果与失败状态。"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import ConfigField, ConfigSchema, LayeredConfigLoader
from app.plugins import PluginManager, PluginSpec

log = logging.getLogger("app.api")

DEFAULT_SCHEMA = ConfigSchema(
    fields=(
        ConfigField("app.name", "str", required=True),
        ConfigField("http.port", "int", default=8080),
        ConfigField("http.debug", "bool", default=False),
    )
)


def _env_source(prefix: str = "APP_") -> dict[str, object]:
    """从环境变量读取点分键：APP_HTTP__PORT -> http.port。"""
    out: dict[str, object] = {}
    for raw, val in os.environ.items():
        if raw.startswith(prefix):
            key = raw[len(prefix):].lower().replace("__", ".")
            out[key] = val
    return out


def load_app_config(
    overrides: dict[str, object] | None = None,
    schema: ConfigSchema = DEFAULT_SCHEMA,
    include_env: bool = True,
):
    """固定优先级（高 -> 低）：external(入参覆盖) > env > builtin 缺省。

    注意：环境变量的值恒为字符串，仅适合 ``str`` 配置项；
    int/float/bool 项应由结构化的 external 覆盖提供（如文件/CLI 解析层）。
    """
    sources: list[tuple[str, dict[str, object]]] = []
    if overrides:
        sources.append(("external", overrides))
    if include_env:
        env = _env_source()
        if env:
            sources.append(("env", env))
    sources.append(("builtin", {}))
    return LayeredConfigLoader(schema).load(sources)


def create_app(
    specs: Iterable[PluginSpec] = (),
    overrides: dict[str, object] | None = None,
    manager: PluginManager | None = None,
) -> FastAPI:
    """创建应用：加载配置、注册插件、lifespan 内完成装配与卸载。"""
    cfg = load_app_config(overrides, include_env=False)
    pm = manager or PluginManager()
    for spec in specs:
        pm.register(spec)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        result = await pm.assemble()
        log.info("启动装配完成 initialized=%s failed=%s",
                 result.initialized, [r.plugin for r in result.failed])
        yield
        await pm.teardown()

    app = FastAPI(title="fastapi-plugin-kernel", lifespan=lifespan)
    app.state.config = cfg
    app.state.plugins = pm

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, object]:
        result = pm.result()
        return {
            "status": "degraded" if result.failed else "ready",
            "initialized": list(result.initialized),
            "failed": [
                {"plugin": r.plugin, "error_type": r.error_type, "message": r.message}
                for r in result.failed
            ],
            "skipped": list(result.skipped),
            "order": list(result.order),
        }

    @app.get("/config/{key:path}")
    async def get_config(key: str) -> dict[str, object]:
        if key not in cfg.values:
            raise HTTPException(status_code=404, detail=f"未知配置项: {key}")
        return {
            "key": key,
            "value": cfg.get(key),
            "source": cfg.sources.get(key, "default"),
            "explanation": cfg.explain(key),
        }

    @app.get("/plugins/{name}")
    async def plugin_status(name: str) -> dict[str, object]:
        result = pm.result()
        if name not in result.order:
            raise HTTPException(status_code=404, detail=f"插件未注册: {name}")
        rec = result.failure_of(name)
        return {
            "name": name,
            "healthy": result.is_healthy(name),
            "state": "failed" if rec else (
                "skipped" if name in result.skipped else "initialized"
            ),
            "error": (
                {"type": rec.error_type, "message": rec.message} if rec else None
            ),
        }

    return app


# 进程入口（main:app）使用的默认应用：无插件，配置仅由 external + 内置缺省构成。
# 必填项 app.name 由部署方通过结构化 external 覆盖提供。
app = create_app(overrides={"app.name": "kernel"})
