"""对外查询接口：配置解析结果、插件状态快照与热重载报告。"""
from __future__ import annotations

from fastapi import APIRouter, Request


def _status_dict(s) -> dict:
    """插件状态的对外表示（保持原有三个字段不变）。"""
    return {"name": s.name, "state": s.state.value, "error": s.error}


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/config")
    def get_config(request: Request) -> dict:
        """返回每个配置项的最终值、生效来源与被覆盖记录。"""
        resolved = request.app.state.resolved_config
        return {
            key: {
                "value": rv.value,
                "source": rv.source,
                "overridden": [
                    {"source": s, "value": v} for s, v in rv.overridden
                ],
            }
            for key, rv in resolved.items()
        }

    @router.get("/plugins")
    def list_plugins(request: Request) -> dict:
        """返回全部插件当前轮的完整状态快照（含失败/被牵连原因）。"""
        manager = request.app.state.plugin_manager
        return {"plugins": [_status_dict(s) for s in manager.statuses()]}

    @router.get("/plugins/{name}")
    def get_plugin(name: str, request: Request) -> dict:
        manager = request.app.state.plugin_manager
        status = manager.status(name)
        if status is None:
            return {"name": name, "state": "unknown", "error": None}
        return _status_dict(status)

    @router.get("/reload/report")
    def get_reload_report(request: Request) -> dict:
        """返回最近一轮热重载的变更报告（含卸载阶段错误留痕）。"""
        report = request.app.state.plugin_manager.last_report()
        return {
            "started": list(report.started),
            "replaced": list(report.replaced),
            "removed": list(report.removed),
            "kept": list(report.kept),
            "failed": [
                {"name": s.name, "error": s.error} for s in report.failed
            ],
            "blocked": [
                {
                    "name": s.name,
                    "blocked_by": list(s.blocked_by),
                    "error": s.error,
                }
                for s in report.blocked
            ],
            "dispose_failures": [
                {"name": f.name, "error": f.error} for f in report.dispose_failures
            ],
        }

    return router
