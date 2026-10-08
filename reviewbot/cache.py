"""Redis 缓存、分布式锁与 singleflight。

v1 的三个问题在这里被修掉：
1. 缓存 key 只做 ``strip()``，无语义归一化 → 改为 AST 指纹 + 分析器版本 + 模型版本；
2. 写了 ``acquire_lock`` 但**从未调用** → 这里真正接进提交路径；
3. ``release_lock`` 是裸 ``DELETE`` → 改为 token 校验 + Lua 原子释放，
   否则锁 TTL 到期后可能误删其他 worker 持有的锁。

本模块提供**两个**实现，刻意共享 key 结构与 Lua 脚本：

* :class:`AsyncCacheStore` —— 给 API/流水线用（``redis.asyncio``）。
  流水线跑在事件循环里，同步客户端每次调用都会**阻塞事件循环**，
  一次网络往返期间整个进程停摆，高并发下吞吐会退化成近似串行。
* :class:`CacheStore` —— 给 Celery worker 用（同步客户端）。
  worker 进程里没有事件循环要保护，用同步版更简单直接。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import redis
import redis.asyncio as redis_async

from reviewbot.logging_setup import get_logger

logger = get_logger(__name__)

#: `redis.asyncio.Redis` 的最小类型别名，便于测试注入替身
AsyncRedis = redis_async.Redis

CACHE_PREFIX = "crb:cache"
LOCK_PREFIX = "crb:lock"
RESULT_PREFIX = "crb:result"

# 只有「值是自己的 token」时才删除，保证不会误删他人的锁
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def build_cache_key(*, language: str, normalized_fingerprint: str, analyzer_version: str, model: str) -> str:
    """缓存 key = 语言 + AST 指纹 + 分析器版本 + 模型版本。

    把 ``model`` 与 ``analyzer_version`` 纳入 key，是为了避免 v1 的一个隐患：
    升级模型或修改分析逻辑后，用户仍然拿到旧结果却毫无察觉。
    """
    raw = "|".join([language, normalized_fingerprint, analyzer_version, model])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:{digest}"


class AsyncCacheStore:
    """`redis.asyncio` 版本的缓存与锁。

    **为什么需要它**：流水线跑在事件循环里，而同步 Redis 客户端每次调用都会阻塞
    事件循环——一次 `GET` 在网络往返期间会让整个进程停止处理其他请求。
    高并发下这会把吞吐压到接近"串行"。

    与同步版共享同一套 key 结构与同一段 Lua 释放脚本（见模块顶部的
    :func:`build_cache_key` 与 :data:`_RELEASE_LUA`），避免两套实现慢慢漂移。
    """

    def __init__(self, client: AsyncRedis, ttl_seconds: int = 3600, lock_ttl_seconds: int = 45) -> None:
        self.client = client
        self.ttl = ttl_seconds
        self.lock_ttl = lock_ttl_seconds

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self.client.get(key)
        except Exception:  # noqa: BLE001 - 缓存不可用时降级为未命中
            logger.warning("cache_get_failed", key=key, exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("cache_value_corrupted", key=key)
            await self.client.delete(key)
            return None

    async def set(self, key: str, value: dict[str, Any]) -> bool:
        try:
            await self.client.setex(key, self.ttl, json.dumps(value, ensure_ascii=False))
            return True
        except Exception:  # noqa: BLE001 - 写缓存失败不应让主流程失败
            logger.warning("cache_set_failed", key=key, exc_info=True)
            return False

    async def acquire_lock(self, key: str) -> str | None:
        token = uuid.uuid4().hex
        try:
            acquired = await self.client.set(f"{LOCK_PREFIX}:{key}", token, nx=True, ex=self.lock_ttl)
        except Exception:  # noqa: BLE001
            logger.warning("lock_acquire_failed", key=key, exc_info=True)
            return None
        return token if acquired else None

    async def release_lock(self, key: str, token: str) -> bool:
        try:
            released = await self.client.eval(_RELEASE_LUA, 1, f"{LOCK_PREFIX}:{key}", token)
            return bool(released)
        except Exception:  # noqa: BLE001
            logger.warning("lock_release_failed", key=key, exc_info=True)
            return False

    async def put_progress(self, task_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        try:
            await self.client.setex(
                f"{RESULT_PREFIX}:{task_id}", ttl_seconds, json.dumps(payload, ensure_ascii=False)
            )
        except Exception:  # noqa: BLE001
            logger.warning("progress_write_failed", task_id=task_id, exc_info=True)

    async def get_progress(self, task_id: str) -> dict[str, Any] | None:
        try:
            raw = await self.client.get(f"{RESULT_PREFIX}:{task_id}")
        except Exception:  # noqa: BLE001
            logger.warning("progress_read_failed", task_id=task_id, exc_info=True)
            return None
        return json.loads(raw) if raw else None


class CacheStore:
    def __init__(self, client: redis.Redis, ttl_seconds: int = 3600, lock_ttl_seconds: int = 45) -> None:
        self.client = client
        self.ttl = ttl_seconds
        self.lock_ttl = lock_ttl_seconds

    # ---------- 结果缓存 ----------
    def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = self.client.get(key)
        except Exception:  # noqa: BLE001
            # 缓存不可用时降级为「未命中」，让主流程继续，而不是直接 500。
            # 这里刻意捕获宽泛异常：Redis 客户端可能抛 RedisError，
            # 也可能是连接层抛出的 OSError/ConnectionError，甚至是替身实现的自定义异常。
            logger.warning("cache_get_failed", key=key, exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("cache_value_corrupted", key=key)
            self.client.delete(key)
            return None

    def set(self, key: str, value: dict[str, Any]) -> bool:
        try:
            self.client.setex(key, self.ttl, json.dumps(value, ensure_ascii=False))
            return True
        except Exception:  # noqa: BLE001 - 写缓存失败不应让主流程失败
            logger.warning("cache_set_failed", key=key, exc_info=True)
            return False

    # ---------- 分布式锁（防缓存击穿）----------
    def acquire_lock(self, key: str) -> str | None:
        """返回持有的 token，失败返回 ``None``。"""
        token = uuid.uuid4().hex
        try:
            acquired = self.client.set(f"{LOCK_PREFIX}:{key}", token, nx=True, ex=self.lock_ttl)
        except Exception:  # noqa: BLE001
            logger.warning("lock_acquire_failed", key=key, exc_info=True)
            return None
        return token if acquired else None

    def release_lock(self, key: str, token: str) -> bool:
        """仅当锁仍属于本 token 时释放；必须原子，否则存在经典竞态。"""
        try:
            released = self.client.eval(_RELEASE_LUA, 1, f"{LOCK_PREFIX}:{key}", token)
            return bool(released)
        except Exception:  # noqa: BLE001
            logger.warning("lock_release_failed", key=key, exc_info=True)
            return False

    # ---------- 任务进度（替代 v1 往 Celery backend 里塞结果的做法）----------
    def put_progress(self, task_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        try:
            self.client.setex(f"{RESULT_PREFIX}:{task_id}", ttl_seconds, json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            logger.warning("progress_write_failed", task_id=task_id, exc_info=True)

    def get_progress(self, task_id: str) -> dict[str, Any] | None:
        try:
            raw = self.client.get(f"{RESULT_PREFIX}:{task_id}")
        except Exception:  # noqa: BLE001
            logger.warning("progress_read_failed", task_id=task_id, exc_info=True)
            return None
        return json.loads(raw) if raw else None
