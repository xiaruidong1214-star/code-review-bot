"""Celery 应用（可选组件）。

设计说明：Celery 是**可选**的。当 ``CRB_CELERY_BROKER_URL`` 为空时，
API 进程直接用 ``asyncio.create_task`` 在进程内跑流水线，
这样单机演示不需要额外起 worker；配置了 broker 则交给 Celery。
"""

from __future__ import annotations

import asyncio

from reviewbot.logging_setup import configure_logging, get_logger
from reviewbot.settings import get_settings

logger = get_logger(__name__)

try:  # pragma: no cover - 取决于是否安装 celery extra
    from celery import Celery

    _CELERY_AVAILABLE = True
except ImportError:  # pragma: no cover
    Celery = None  # type: ignore[assignment]
    _CELERY_AVAILABLE = False


def build_celery_app():
    """按配置构造 Celery 应用；未启用或未安装时返回 ``None``。"""
    settings = get_settings()
    if not settings.celery_enabled or not _CELERY_AVAILABLE:
        return None

    app = Celery(
        "code_review_bot",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend or None,
        include=["reviewbot.tasks"],
    )
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="Asia/Shanghai",
        enable_utc=True,
        task_acks_late=True,  # 任务被 kill 时不丢消息，配合幂等写入
        worker_prefetch_multiplier=1,  # LLM 是长任务，禁止预取堆积
        task_time_limit=600,
        task_soft_time_limit=540,
        result_expires=settings.result_ttl_seconds,
    )
    return app


celery_app = build_celery_app()


def run_async(coro):
    """在同步 Celery 任务里执行协程。

    每个 worker 进程自建一次事件循环，避免与 API 进程的事件循环耦合。
    """
    configure_logging()
    return asyncio.run(coro)
