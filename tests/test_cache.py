"""缓存 key、锁与防击穿逻辑测试。

v1 的三个问题在这里都必须被证明已修复：
1. ``strip()`` 之外的语义归一化（注释/空白/换行风格不应影响命中）；
2. ``acquire_lock`` 真的会被调用（在 runner 测试里验证）；
3. 释放锁必须校验 token，否则会误删他人的锁。
"""

from __future__ import annotations

import json

from reviewbot.cache import CACHE_PREFIX, LOCK_PREFIX, build_cache_key


def _key(**overrides):
    base = {
        "language": "python",
        "normalized_fingerprint": "fp-1",
        "analyzer_version": "2.0.0",
        "model": "deepseek-chat",
    }
    base.update(overrides)
    return build_cache_key(**base)


def test_cache_key_is_deterministic():
    assert _key() == _key()


def test_cache_key_changes_with_each_component():
    baseline = _key()
    assert _key(language="java") != baseline
    assert _key(normalized_fingerprint="fp-2") != baseline
    # 升级分析器或模型后必须换 key，否则用户会拿到旧逻辑的结果却毫无察觉
    assert _key(analyzer_version="2.1.0") != baseline
    assert _key(model="deepseek-reasoner") != baseline


def test_cache_key_prefix():
    assert _key().startswith(f"{CACHE_PREFIX}:")


def test_cache_roundtrip(cache):
    key = _key()
    assert cache.get(key) is None
    assert cache.set(key, {"analysis": {"x": 1}, "llm": {"summary": "s"}})
    assert cache.get(key) == {"analysis": {"x": 1}, "llm": {"summary": "s"}}


def test_corrupted_cache_value_is_dropped(cache, fake_redis):
    key = _key()
    fake_redis.values[key] = "{不是合法 JSON"
    assert cache.get(key) is None
    # 坏值应被主动清理，避免每次请求都重复解析失败
    assert key not in fake_redis.values


def test_cache_ttl_makes_entry_disappear(cache, fake_redis):
    key = _key()
    cache.set(key, {"ok": True})
    assert cache.get(key) == {"ok": True}
    fake_redis.expiry[key] = 0.0  # 模拟过期
    assert cache.get(key) is None


def test_lock_is_exclusive(cache):
    key = _key()
    token = cache.acquire_lock(key)
    assert token is not None
    assert cache.acquire_lock(key) is None  # 第二个持有者必须失败


def test_release_lock_requires_matching_token(cache):
    key = _key()
    token = cache.acquire_lock(key)
    assert token is not None
    # 用错误的 token 释放必须失败，且锁仍然存在
    assert cache.release_lock(key, "别人的-token") is False
    assert cache.acquire_lock(key) is None
    # 正确的 token 才能释放
    assert cache.release_lock(key, token) is True
    assert cache.acquire_lock(key) is not None


def test_lock_can_be_reacquired_after_expiry(cache, fake_redis):
    key = _key()
    assert cache.acquire_lock(key) is not None
    fake_redis.expiry[f"{LOCK_PREFIX}:{key}"] = 0.0
    assert cache.acquire_lock(key) is not None


def test_progress_roundtrip(cache):
    cache.put_progress("t-1", {"task_id": "t-1", "progress": 30}, 60)
    assert cache.get_progress("t-1")["progress"] == 30
    assert cache.get_progress("missing") is None


def test_redis_error_degrades_get_to_miss(cache, fake_redis):
    """Redis 报错时 get 应降级为未命中，而不是把异常抛给请求方。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("redis down")

    fake_redis.get = boom  # type: ignore[method-assign]
    assert cache.get("any") is None


def test_json_payload_survives_non_ascii(cache):
    key = _key()
    payload = {"llm": {"summary": "中文摘要 ✅", "suggestions": ["建议一"]}}
    cache.set(key, payload)
    assert cache.get(key) == json.loads(json.dumps(payload, ensure_ascii=False))
