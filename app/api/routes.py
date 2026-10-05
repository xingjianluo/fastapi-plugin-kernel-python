"""对外查询接口：配置解析结果与插件装配状态。"""
from __future__ import annotations

from fastapi import APIRouter, Request


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
        """返回全部插件的装配状态快照（含失败原因）。"""
        assembler = request.app.state.assembler
        return {
            "plugins": [
                {"name": s.name, "state": s.state.value, "error": s.error}
                for s in assembler.statuses()
            ]
        }

    @router.get("/plugins/{name}")
    def get_plugin(name: str, request: Request) -> dict:
        assembler = request.app.state.assembler
        status = assembler.status(name)
        if status is None:
            return {"name": name, "state": "unknown", "error": None}
        return {"name": status.name, "state": status.state.value, "error": status.error}

    @router.post("/reload")
    def reload_plugins(request: Request) -> dict:
        """从插件提供者重新取一组插件，当场热重载并返回变更报告。"""
        reloader = request.app.state.reloader
        provider = request.app.state.plugin_provider
        report = reloader.reload(provider())
        return _report_to_dict(report)

    @router.get("/reload/report")
    def last_reload_report(request: Request) -> dict:
        """返回最近一轮热重载的变更报告；未重载过则为空。"""
        reloader = request.app.state.reloader
        report = reloader.last_report()
        if report is None:
            return {"reloaded": False, "changes": [], "dispose_errors": [], "summary": ""}
        return {"reloaded": True, **_report_to_dict(report)}

    return router


def _report_to_dict(report) -> dict:
    return {
        "changes": [
            {"name": c.name, "kind": c.kind.value, "reason": c.reason}
            for c in report.changes
        ],
        "dispose_errors": [
            {"name": n, "error": e} for n, e in report.dispose_errors
        ],
        "summary": report.summary(),
    }
