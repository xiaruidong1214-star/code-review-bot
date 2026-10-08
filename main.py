"""`reviewbot.api` 的启动入口：`uvicorn main:app`。"""

from reviewbot.api import app, build_app

__all__ = ["app", "build_app"]
