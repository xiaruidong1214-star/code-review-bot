"""依赖装配。

集中在一个地方创建并持有长生命周期对象（Redis 连接、httpx 客户端、SQLite 连接），
避免像 v1 那样在模块导入时用 ``redis.from_url`` 直接建全局单例——
那种写法让测试无法注入替身，也让配置在导入期就被冻结。
"""

from __future__ import annotations

import redis
import redis.asyncio as redis_async

from reviewbot.cache import AsyncCacheStore
from reviewbot.github_source import GitHubFetcher
from reviewbot.llm import LLMClient
from reviewbot.logging_setup import configure_logging, get_logger
from reviewbot.runner import ReviewPipeline
from reviewbot.settings import Settings, get_settings
from reviewbot.store import ReviewStore

logger = get_logger(__name__)


class Container:
    """应用容器：负责创建与释放所有长生命周期资源。"""

    def __init__(self, settings: Settings | None = None, transport=None) -> None:
        self.settings = settings or get_settings()
        configure_logging(self.settings.log_level, self.settings.log_json)

        # 同步客户端：给 Celery worker 与健康检查这类同步调用用
        self.redis = redis.from_url(self.settings.redis_url, decode_responses=True)
        # 异步客户端：给流水线用。**必须**用异步版，否则每次缓存读写都会
        # 阻塞事件循环（见 reviewbot/cache.py 顶部的说明）。
        self.async_redis = redis_async.from_url(self.settings.redis_url, decode_responses=True)
        self.cache = AsyncCacheStore(
            self.async_redis,
            ttl_seconds=self.settings.cache_ttl_seconds,
            lock_ttl_seconds=self.settings.lock_ttl_seconds,
        )
        self.store = ReviewStore(self.settings.db_path, self.settings.max_stored_code_chars)
        self.llm = LLMClient(
            base_url=self.settings.llm_base_url,
            model=self.settings.llm_model,
            api_key=self.settings.llm_api_key,
            timeout=self.settings.llm_timeout_seconds,
            max_retries=self.settings.llm_max_retries,
            max_input_chars=self.settings.llm_max_input_chars,
            transport=transport,
        )
        self.github = GitHubFetcher(
            allowed_hosts=self.settings.github_allowed_hosts,
            token=self.settings.github_token,
            max_bytes=self.settings.github_max_file_bytes,
            timeout=self.settings.github_timeout_seconds,
            transport=transport,
        )
        self.pipeline = ReviewPipeline(
            settings=self.settings,
            store=self.store,
            cache=self.cache,
            llm=self.llm,
            github=self.github,
        )

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.github.aclose()
        try:
            await self.async_redis.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不应影响进程退出
            logger.warning("async_redis_close_failed", exc_info=True)

    def close(self) -> None:
        self.store.close()
        try:
            self.redis.close()
        except Exception:  # noqa: BLE001 - 关闭失败不应影响进程退出
            logger.warning("redis_close_failed", exc_info=True)


def build_pipeline() -> ReviewPipeline:
    """供 Celery worker 使用：每次任务自建容器（worker 进程内可缓存优化）。"""
    return Container().pipeline
