"""审查流水线：缓存 → 分布式锁 → 静态分析 → LLM → 落库。

关键设计（针对 v1 的实际缺陷）：

* **防缓存击穿是真的**：v1 写了 ``acquire_lock`` 却从未调用。这里锁进主路径，
  拿不到锁的请求进入 singleflight 等待，而不是一起去打上游 LLM。
* **失败不再伪装成成功**：LLM 降级会被显式记录为 ``degraded`` 并单独计数，
  而不是像 v1 那样返回一句"分析暂不可用"却记 SUCCESS。
* **基础设施故障 = 任务失败**：Redis 挂掉时抛异常让任务变 FAILED，
  而不是静默降级成"未命中"然后重复烧钱。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from reviewbot.analysis import analyze_python, normalize_source
from reviewbot.cache import CacheStore, build_cache_key
from reviewbot.github_source import GitHubFetcher, GitHubFetchError
from reviewbot.llm import LLMClient
from reviewbot.logging_setup import get_logger
from reviewbot.store import ReviewStore

logger = get_logger(__name__)

# 改动分析逻辑或提示词时必须递增：它参与缓存 key，避免旧结果被继续命中
ANALYZER_VERSION = "2.0.0"
PROMPT_VERSION = "2.0.0"

ProgressCallback = Callable[[int, str], Awaitable[None]]


@dataclass(slots=True)
class LLMOutcome:
    summary: str
    suggestions: list[str] = field(default_factory=list)
    degraded: bool = False
    error: str | None = None
    total_tokens: int = 0


@dataclass(slots=True)
class PipelineResult:
    analysis: dict[str, Any]
    llm: LLMOutcome
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis": self.analysis,
            "llm": asdict(self.llm),
            "cache_hit": self.cache_hit,
        }


def _structure_note(analysis: dict[str, Any]) -> str:
    structure = analysis.get("structure") or {}
    recursion = analysis.get("recursion") or {}
    return (
        f"最大循环嵌套深度={structure.get('max_loop_nesting')}, "
        f"循环数={structure.get('loop_count')}, "
        f"函数数={structure.get('function_count')}, "
        f"注释率={structure.get('comment_ratio')}%, "
        f"存在递归={recursion.get('has_recursion')}"
    )


class ReviewPipeline:
    def __init__(
        self,
        *,
        settings,
        store: ReviewStore,
        cache: CacheStore,
        llm: LLMClient,
        github: GitHubFetcher,
    ) -> None:
        self.settings = settings
        self.store = store
        self.cache = cache
        self.llm = llm
        self.github = github

    # ---------------- 提交 ----------------
    def submit(self, *, code: str | None, github_url: str | None, language: str) -> tuple[str, str, bool]:
        """创建任务记录并立即返回（不阻塞请求）。

        返回 ``(task_id, source_type, cache_hit)``。命中缓存时结果已经可直接读取。
        """
        task_id = str(uuid.uuid4())
        source_type = "code" if code else "github"

        if github_url:
            self.store.create(task_id, language, github_url, source_type)
            return task_id, source_type, False

        assert code is not None
        self.store.create(task_id, language, code, source_type)

        fingerprint = normalize_source(code)
        if fingerprint is None:
            # 语法错误的代码不缓存：无论如何重试都只会得到同一个解析错误
            return task_id, source_type, False

        cache_key = build_cache_key(
            language=language,
            normalized_fingerprint=fingerprint,
            analyzer_version=ANALYZER_VERSION,
            model=self.llm.model,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.info("cache_hit", task_id=task_id)
            stored = dict(cached)
            stored["cache_hit"] = True
            self.store.finish(
                task_id,
                state="SUCCESS",
                result=stored,
                cache_hit=True,
                llm_degraded=bool((stored.get("llm") or {}).get("degraded")),
            )
            return task_id, source_type, True

        return task_id, source_type, False

    # ---------------- 执行 ----------------
    async def run(
        self,
        *,
        task_id: str,
        code: str | None = None,
        github_url: str | None = None,
        language: str = "python",
        on_progress: ProgressCallback | None = None,
    ) -> PipelineResult:
        async def report(progress: int, step: str) -> None:
            if on_progress is not None:
                await on_progress(progress, step)

        try:
            if github_url:
                await report(5, "抓取 GitHub 源码")
                code = await self.github.fetch(github_url)
            if not code:
                raise ValueError("没有可供分析的代码")

            cache_key = self._cache_key(code, language)
            fingerprint = normalize_source(code)

            # 1) 缓存已在上游 submit 查过，这里再查一次以覆盖「等待期间他人已写入」的情况
            cached = self.cache.get(cache_key) if cache_key else None
            if cached is not None:
                await report(100, "命中缓存")
                result = PipelineResult(
                    analysis=cached["analysis"], llm=LLMOutcome(**cached["llm"]), cache_hit=True
                )
                self._persist(task_id, result)
                return result

            # 2) 抢锁；抢不到就等别人算完（singleflight）
            if cache_key and fingerprint is not None:
                token = self.cache.acquire_lock(cache_key)
                if token is None:
                    await report(20, "同类请求正在分析，等待结果")
                    waited = await self._wait_for_cache(cache_key)
                    if waited is not None:
                        result = PipelineResult(
                            analysis=waited["analysis"], llm=LLMOutcome(**waited["llm"]), cache_hit=True
                        )
                        self._persist(task_id, result)
                        return result
                    logger.info("singleflight_timeout", task_id=task_id)
                    token = self.cache.acquire_lock(cache_key)

                try:
                    result = await self._analyze(task_id, code, language, report)
                finally:
                    if token is not None:
                        # 只在锁仍属于自己时释放，避免误删他人的锁
                        self.cache.release_lock(cache_key, token)
            else:
                result = await self._analyze(task_id, code, language, report)

            # 3) 写缓存：让等待者与后续重复请求直接命中
            if cache_key and fingerprint is not None:
                self.cache.set(cache_key, result.to_dict())

            await report(100, "完成")
            self._persist(task_id, result)
            return result

        except asyncio.CancelledError:
            self.store.finish(task_id, state="FAILED", result=None, error="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 —— 这里必须兜底并如实落库
            logger.exception("review_failed", task_id=task_id)
            message = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, GitHubFetchError):
                message = f"GitHub 抓取失败：{exc}"
            self.store.finish(task_id, state="FAILED", result=None, error=message)
            raise

    # ---------------- 内部 ----------------
    def _cache_key(self, code: str, language: str) -> str | None:
        fingerprint = normalize_source(code)
        if fingerprint is None:
            return None
        return build_cache_key(
            language=language,
            normalized_fingerprint=fingerprint,
            analyzer_version=ANALYZER_VERSION,
            model=self.llm.model,
        )

    async def _wait_for_cache(self, cache_key: str) -> dict[str, Any] | None:
        """轮询等待持锁者写入结果；超时后返回 ``None`` 由调用方自行分析。"""
        deadline = time.monotonic() + self.settings.singleflight_wait_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            found = self.cache.get(cache_key)
            if found is not None:
                logger.info("singleflight_satisfied")
                return found
        return None

    async def _analyze(
        self,
        task_id: str,
        code: str,
        language: str,
        report: ProgressCallback,
    ) -> PipelineResult:
        await report(30, "静态结构分析")
        # 静态分析是纯 CPU 工作，放到线程里避免阻塞事件循环
        result = await asyncio.to_thread(analyze_python, code)
        analysis = {
            "language": result.language,
            "structure": asdict(result.structure),
            "recursion": {
                "has_recursion": result.recursion.has_recursion,
                "direct_recursive": result.recursion.direct_recursive,
                "mutually_recursive_groups": result.recursion.mutually_recursive_groups,
            },
            "findings": [asdict(f) for f in result.findings],
            "parse_error": result.parse_error,
        }

        if result.parse_error:
            # 语法错误是「分析成功、代码有问题」，不是任务失败
            await report(100, "解析失败")
            return PipelineResult(
                analysis=analysis,
                llm=LLMOutcome(
                    summary="代码无法解析，已跳过模型分析",
                    suggestions=[],
                    degraded=True,
                    error=result.parse_error,
                ),
            )

        await report(70, "调用大模型分析")
        review = await self.llm.review(code, _structure_note(analysis))
        return PipelineResult(
            analysis=analysis,
            llm=LLMOutcome(
                summary=review.summary,
                suggestions=review.suggestions,
                degraded=review.degraded,
                error=review.error,
                total_tokens=review.total_tokens,
            ),
        )

    def _persist(self, task_id: str, result: PipelineResult) -> None:
        self.store.finish(
            task_id,
            state="SUCCESS",
            result=result.to_dict(),
            cache_hit=result.cache_hit,
            llm_degraded=result.llm.degraded,
            error=result.llm.error if result.llm.degraded else None,
        )


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
