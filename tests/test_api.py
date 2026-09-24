"""API 集成测试：启动装配、失败隔离降级、配置/插件状态查询。"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.api import create_app
from app.plugins import Plugin, PluginSpec

log = logging.getLogger("test.api")


class OkPlugin(Plugin):
    name = "ok"
    dependencies = ()
    torn = 0

    async def initialize(self, ctx) -> None:
        log.info("输入插件 ok 初始化；判定依据：无依赖，直接成功")

    async def teardown(self) -> None:
        OkPlugin.torn += 1
        log.info("插件 ok 释放（第 %s 次）", OkPlugin.torn)


class BoomPlugin(Plugin):
    name = "boom"
    dependencies = ()

    async def initialize(self, ctx) -> None:
        raise RuntimeError("api-boom-reason")

    async def teardown(self) -> None:
        pass


def _specs():
    return [
        PluginSpec("ok", lambda: OkPlugin()),
        PluginSpec("boom", lambda: BoomPlugin()),
    ]


def test_lifespan_assembles_and_service_stays_available() -> None:
    OkPlugin.torn = 0
    app = create_app(specs=_specs(), overrides={"app.name": "demo", "http.port": 9000})
    with TestClient(app) as client:  # 触发 lifespan -> assemble/teardown
        log.info("装配输入：ok + boom；判定依据：boom 失败隔离，服务仍可用")

        health = client.get("/healthz").json()
        assert health == {"status": "ok"}

        ready = client.get("/readyz").json()
        log.info("装配顺序=%s 判定结果=%s", ready["order"], ready)
        assert ready["status"] == "degraded"
        assert ready["initialized"] == ["ok"]
        assert ready["failed"][0]["plugin"] == "boom"
        assert ready["failed"][0]["error_type"] == "RuntimeError"
        assert ready["failed"][0]["message"] == "api-boom-reason"

        status = client.get("/plugins/boom").json()
        assert status["healthy"] is False
        assert status["error"]["message"] == "api-boom-reason"
        assert client.get("/plugins/ok").json()["healthy"] is True
        assert client.get("/plugins/nope").status_code == 404

    # 退出 with 后 lifespan 已完成逆序卸载：ok 恰好释放一次
    assert OkPlugin.torn == 1
    log.info("关闭后 teardown 次数=%s，判定依据：initialize 成功者各释放一次", OkPlugin.torn)


def test_config_endpoint_explains_source_and_default() -> None:
    app = create_app(
        specs=[],
        overrides={"app.name": "demo", "http.port": 9000},
    )
    with TestClient(app) as client:
        port = client.get("/config/http.port").json()
        log.info("输入 external=http.port:9000，判定依据=%s", port["explanation"])
        assert port["value"] == 9000
        assert port["source"] == "external"

        debug = client.get("/config/http.debug").json()
        assert debug["value"] is False
        assert debug["source"] == "default"
        assert "声明缺省值" in debug["explanation"]

        assert client.get("/config/unknown.key").status_code == 404


def test_missing_required_config_rejects_app_construction() -> None:
    import pytest

    from app.config import MissingRequiredError

    log.info("输入：不提供必填 app.name；判定依据：MissingRequiredError，服务拒绝启动")
    with pytest.raises(MissingRequiredError):
        create_app(specs=[], overrides={"http.port": 1})
