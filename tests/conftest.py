"""测试公共设施：无需 Redis / 网络即可覆盖核心逻辑。"""

from __future__ import annotations

import fnmatch
import json
import time
from typing import Any

import pytest

from reviewbot.cache import CacheStore
from reviewbot.llm import LLMClient
from reviewbot.settings import Settings


class FakeRedis:
    """手写的最小 Redis 替身。

    为什么不直接用 fakeredis：本仓库用到的 ``SET NX`` 与 Lua 释放脚本语义
    需要精确可控，自己实现一遍反而更能证明被测代码的真实行为。
    这也顺带说明一件事——代码只依赖很小的 Redis 表面，才可能这样替身。
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiry: dict[str, float] = {}

    # -- 内部 --
    def _expired(self, key: str) -> bool:
        deadline = self.expiry.get(key)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            self.values.pop(key, None)
            self.expiry.pop(key, None)
            return True
        return False

    def _live(self, pattern: str) -> list[str]:
        for key in list(self.values):
            self._expired(key)
        return [k for k in self.values if fnmatch.fnmatchcase(k, pattern)]

    # -- 被测代码用到的接口 --
    def get(self, key: str) -> str | None:
        if self._expired(key):
            return None
        return self.values.get(key)

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and self.get(key) is not None:
            return None
        self.values[key] = value
        if ex is not None:
            self.expiry[key] = time.monotonic() + ex
        return True

    def setex(self, key: str, ttl: int, value: str) -> bool:
        self.values[key] = value
        self.expiry[key] = time.monotonic() + ttl
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.values.pop(key, None) is not None:
                removed += 1
            self.expiry.pop(key, None)
        return removed

    def keys(self, pattern: str = "*") -> list[str]:
        return self._live(pattern)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self.values.clear()

    def eval(self, script: str, numkeys: int, *args: Any):
        """只实现本项目用到的那一个释放锁脚本。"""
        if "redis.call('get', KEYS[1]) == ARGV[1]" not in script:
            raise NotImplementedError("FakeRedis 只实现了释放锁脚本")
        key, token = args[0], args[1]
        if self.get(key) == token:
            self.delete(key)
            return 1
        return 0


class FakeTransport:
    """httpx MockTransport 的包装，按 URL 片段分发响应。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, int, dict]] = []
        self.calls: list[dict] = []

    def add(self, match: str, payload: dict, status: int = 200) -> None:
        self.routes.append((match, status, payload))

    def handler(self, request):  # pragma: no cover - 由 httpx 调用
        import httpx

        self.calls.append({"url": str(request.url), "body": request.content})
        for match, status, payload in self.routes:
            if match in str(request.url):
                return httpx.Response(status, json=payload, request=request)
        return httpx.Response(404, json={"error": "no route"}, request=request)

    def as_transport(self):
        import httpx

        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def cache(fake_redis: FakeRedis) -> CacheStore:
    return CacheStore(fake_redis, ttl_seconds=60, lock_ttl_seconds=5)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "test.db"),
        llm_api_key="",
        redis_url="redis://localhost:6379/15",
        singleflight_wait_seconds=2.0,
        lock_ttl_seconds=5,
        cache_ttl_seconds=60,
        result_ttl_seconds=60,
    )


def make_llm(review_payload: dict | None = None, status: int = 200) -> tuple[LLMClient, FakeTransport]:
    transport = FakeTransport()
    transport.add(
        "/chat/completions",
        review_payload
        or {
            "model": "deepseek-chat",
            "choices": [{"message": {"content": json.dumps({"summary": "看起来不错", "suggestions": ["加类型注解"]})}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        },
        status=status,
    )
    client = LLMClient(
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        api_key="test-key",
        max_retries=0,
        transport=transport.as_transport(),
    )
    return client, transport
