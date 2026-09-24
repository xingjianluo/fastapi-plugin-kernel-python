"""服务入口：uvicorn main:app。"""

from app.api import app

__all__ = ["app"]
