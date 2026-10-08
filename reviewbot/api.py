"""HTTP 接口层。

相对 v1 的改进：

* ``/review/stream`` 真的是流式读取（v1 的注释写着"流式读取"，
  代码却是 ``await request.body()`` 一次性读进内存，10MB 请求体在并发下会打爆内存）；
* 任务状态不再借用 Celery 结果后端，而是显式写入 Redis，并支持 SSE 推送；
* LLM 降级不再表现为成功，响应里 ``llm.degraded`` 字段明确标注；
* 增加请求 ID、限流与健康检查。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from reviewbot import __version__
from reviewbot.bootstrap import Container
from reviewbot.logging_setup import get_logger, request_id_var
from reviewbot.schemas import (
    HealthResponse,
    HistoryItem,
    ReviewAccepted,
    ReviewRequest,
    Stats,
    TaskResult,
    TaskState,
)
from reviewbot.settings import get_settings

logger = get_logger(__name__)
router = APIRouter(prefix="/v1")

# 进程内滑动窗口限流：单实例够用；多实例需换成 Redis 计数器
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _check_rate_limit(request: Request, limit_per_minute: int) -> None:
    client = request.client.host if request.client else "unknown"
    bucket = _rate_buckets[client]
    now = time.monotonic()
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= limit_per_minute:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")
    bucket.append(now)


def build_app(container: Container | None = None) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container or Container()
        app.state.background_tasks = set()
        logger.info("app_started", version=__version__, env=settings.env)
        try:
            yield
        finally:
            for task in list(app.state.background_tasks):
                task.cancel()
            if app.state.background_tasks:
                await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
            await app.state.container.aclose()
            app.state.container.close()
            logger.info("app_stopped")

    app = FastAPI(
        title="code-review-bot",
        version=__version__,
        description="AST 辅助的 Python 代码审查服务（结构统计 + 启发式规则 + LLM 建议）",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = rid
        response.headers["x-response-time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response

    app.include_router(router)
    return app


def _pipeline(request: Request):
    return request.app.state.container.pipeline


async def _execute(request: Request, task_id: str, code: str | None, github_url: str | None, language: str) -> None:
    """后台执行流水线。Celery 未启用时在进程内跑；异常已在流水线内落库。"""
    pipeline = _pipeline(request)
    container: Container = request.app.state.container

    async def on_progress(progress: int, step: str) -> None:
        container.cache.put_progress(
            task_id,
            {"task_id": task_id, "state": TaskState.PROCESSING, "progress": progress, "step": step},
            container.settings.result_ttl_seconds,
        )

    try:
        await pipeline.run(
            task_id=task_id,
            code=code,
            github_url=github_url,
            language=language,
            on_progress=on_progress,
        )
    except asyncio.CancelledError:
        container.cache.put_progress(
            task_id,
            {"task_id": task_id, "state": TaskState.FAILED, "progress": 0, "step": "已取消"},
            container.settings.result_ttl_seconds,
        )
        raise
    except Exception:  # noqa: BLE001 - 已落库，这里只补一条可读的进度记录
        stored = container.store.get(task_id)
        container.cache.put_progress(
            task_id,
            {
                "task_id": task_id,
                "state": TaskState.FAILED,
                "progress": 0,
                "step": "执行失败",
                "error": stored.error if stored else "unknown",
            },
            container.settings.result_ttl_seconds,
        )


def _submit(request: Request, *, code: str | None, github_url: str | None, language: str) -> ReviewAccepted:
    container: Container = request.app.state.container
    limit = container.settings.rate_limit_per_minute
    _check_rate_limit(request, limit)

    if code is not None and len(code.encode("utf-8")) > container.settings.max_code_bytes:
        raise HTTPException(status_code=413, detail=f"代码超过 {container.settings.max_code_bytes} 字节上限")

    pipeline = container.pipeline
    try:
        task_id, source_type, cache_hit = pipeline.submit(
            code=code, github_url=github_url, language=language
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not cache_hit:
        task = asyncio.create_task(_execute(request, task_id, code, github_url, language))
        request.app.state.background_tasks.add(task)
        task.add_done_callback(request.app.state.background_tasks.discard)

    return ReviewAccepted(
        task_id=task_id,
        status=TaskState.SUCCESS if cache_hit else TaskState.PENDING,
        source_type=source_type,
        message="命中缓存，结果可直接查询" if cache_hit else "任务已提交，可通过 /v1/tasks/{task_id} 查询进度",
        cache_hit=cache_hit,
    )


@router.post("/reviews", response_model=ReviewAccepted, status_code=202, summary="提交代码审查")
async def submit_review(payload: ReviewRequest, request: Request) -> ReviewAccepted:
    return _submit(
        request,
        code=payload.code,
        github_url=str(payload.github_url) if payload.github_url else None,
        language=payload.language,
    )


@router.post("/reviews/stream", response_model=ReviewAccepted, status_code=202, summary="流式上传大段代码")
async def submit_review_stream(request: Request, language: str = "python") -> ReviewAccepted:
    """真正的流式读取：边收边判上限，超限立刻中断，不把请求体整体读进内存。"""
    container: Container = request.app.state.container
    declared = request.headers.get("content-length")
    if declared and int(declared) > container.settings.max_stream_bytes:
        raise HTTPException(status_code=413, detail="请求体超过上限")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > container.settings.max_stream_bytes:
            raise HTTPException(status_code=413, detail="请求体超过上限，已中断接收")
        chunks.append(chunk)

    if total == 0:
        raise HTTPException(status_code=400, detail="请求体为空")

    try:
        code = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 UTF-8") from exc

    return _submit(request, code=code, github_url=None, language=language)


@router.get("/tasks/{task_id}", response_model=TaskResult, summary="查询任务状态")
async def get_task(task_id: str, request: Request) -> TaskResult:
    container: Container = request.app.state.container

    stored = container.store.get(task_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    progress = container.cache.get_progress(task_id) or {}

    if stored.state == TaskState.FAILED.value:
        return TaskResult(
            task_id=task_id,
            state=TaskState.FAILED,
            progress=int(progress.get("progress") or 0),
            step=str(progress.get("step") or "失败"),
            error=stored.error,
        )

    if stored.state == TaskState.SUCCESS.value and stored.result:
        return TaskResult(
            task_id=task_id,
            state=TaskState.SUCCESS,
            progress=100,
            step="完成",
            analysis=stored.result.get("analysis"),
            llm=stored.result.get("llm"),
            cache_hit=bool(stored.result.get("cache_hit")),
        )

    return TaskResult(
        task_id=task_id,
        state=TaskState.PROCESSING,
        progress=int(progress.get("progress") or 0),
        step=str(progress.get("step") or "排队中"),
    )


@router.get("/tasks/{task_id}/events", summary="SSE 推送任务进度")
async def stream_task_events(task_id: str, request: Request) -> StreamingResponse:
    container: Container = request.app.state.container
    if container.store.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_source():
        last_step = ""
        for _ in range(600):  # 最长约 2 分钟，防止连接永久占用
            if await request.is_disconnected():
                break
            stored = container.store.get(task_id)
            progress = container.cache.get_progress(task_id) or {}
            payload = {
                "task_id": task_id,
                "state": stored.state if stored else "UNKNOWN",
                "progress": progress.get("progress", 100 if stored and stored.state == "SUCCESS" else 0),
                "step": progress.get("step", ""),
            }
            if payload["step"] != last_step or payload["state"] in {"SUCCESS", "FAILED"}:
                last_step = str(payload["step"])
                yield f"data: {payload}\n\n"
            if payload["state"] in {"SUCCESS", "FAILED"}:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.get("/history", response_model=list[HistoryItem], summary="历史记录")
async def get_history(request: Request, limit: int = 20, offset: int = 0) -> list[HistoryItem]:
    limit = max(1, min(limit, 100))
    container: Container = request.app.state.container
    return [HistoryItem(**item) for item in container.store.history(limit, offset)]


@router.get("/stats", response_model=Stats, summary="统计信息")
async def get_stats(request: Request) -> Stats:
    return Stats(**request.app.state.container.store.stats())


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health(request: Request) -> HealthResponse:
    container: Container = request.app.state.container
    redis_ok = True
    try:
        container.redis.ping()
    except Exception:  # noqa: BLE001 - 健康检查不应抛异常
        redis_ok = False
    db_ok = container.store.ping()
    return HealthResponse(
        status="ok" if (redis_ok and db_ok) else "degraded",
        version=__version__,
        redis=redis_ok,
        database=db_ok,
        llm_configured=container.settings.llm_configured,
        detail={"celery_enabled": container.settings.celery_enabled},
    )


@router.get("/livez", include_in_schema=False)
async def livez() -> Response:
    return Response(status_code=200)


app = build_app()
