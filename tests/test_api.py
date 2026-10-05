"""接口兼容性测试：热重载上线后，老的查询方式照常能用。"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.plugins.manager import PluginManager
from main import create_app
from tests.test_plugins import FakePlugin

log = logging.getLogger(__name__)


def test_plugin_query_shape_unchanged_after_reload():
    with TestClient(create_app()) as client:
        manager: PluginManager = client.app.state.plugin_manager
        before = client.get("/plugins").json()
        log.info("热重载前 /plugins=%s", before)

        # 运行期热重载：新增一个会失败的插件和一个正常插件
        manager.reload(add=[FakePlugin("extra"), FakePlugin("broken", fail_on_init=True)])
        after = client.get("/plugins").json()
        log.info("热重载后 /plugins=%s", after)

        # 老查询方式的返回结构不变：name/state/error 三个字段
        for entry in after["plugins"]:
            assert set(entry) == {"name", "state", "error"}
        by_name = {p["name"]: p for p in after["plugins"]}
        assert by_name["core"]["state"] == "initialized"  # 老插件不受影响的
        assert by_name["extra"]["state"] == "initialized"
        assert by_name["broken"]["state"] == "failed"
        assert "init exploded" in by_name["broken"]["error"]

        single = client.get("/plugins/core").json()
        assert set(single) == {"name", "state", "error"}
        assert client.get("/plugins/ghost").json() == {
            "name": "ghost", "state": "unknown", "error": None,
        }
        log.info("判定依据: 老端点字段结构、unknown 语义与热重载前完全一致")


def test_reload_report_endpoint_reflects_last_round():
    with TestClient(create_app()) as client:
        manager: PluginManager = client.app.state.plugin_manager
        manager.reload(
            add=[FakePlugin("v2"), FakePlugin("bad", fail_on_init=True)],
            remove=["metrics"],
        )
        report = client.get("/reload/report").json()
        log.info("/reload/report=%s", report)
        assert report["started"] == ["v2"]
        assert report["removed"] == ["metrics"]
        assert "core" in report["kept"]
        assert report["failed"][0]["name"] == "bad"
        assert "init exploded" in report["failed"][0]["error"]
        log.info("判定依据: 报告端点如实反映最近一轮的增/删/留/败")
