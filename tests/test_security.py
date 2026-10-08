"""鉴权测试。

本服务背后是按量计费的 LLM，且能读写 SQLite、能对外抓取。
如果暴露在可达地址上又没有凭据校验，任何能碰到该端口的人都能用掉你的额度。
这批用例锁住三条不变量：

1. 配置了 `CRB_API_KEY` 时，**业务端点**无 key / 错 key 都必须被拒；
2. **健康检查端点始终公开**（编排系统探活不该需要凭据）；
3. 未配置 key 时不校验（本机模式），但 `exposed_without_auth` 必须能识别危险组合。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reviewbot.api import build_app
from reviewbot.bootstrap import Container
from reviewbot.security import API_KEY_HEADER
from reviewbot.settings import Settings
from tests.conftest import FakeRedis

API_KEY = "s3cret-key-for-tests"

#: 需要鉴权的端点：(方法, 路径, 请求体)
PROTECTED = [
    ("POST", "/v1/reviews", {"code": "x = 1\n"}),
    ("POST", "/v1/reviews/stream", None),
    ("GET", "/v1/tasks/whatever", None),
    ("GET", "/v1/tasks/whatever/events", None),
    ("GET", "/v1/history", None),
    ("GET", "/v1/stats", None),
]

PUBLIC = [("GET", "/v1/health", None), ("GET", "/v1/livez", None)]


def _client(tmp_path, monkeypatch, *, api_key: str) -> TestClient:
    from tests.conftest import FakeTransport

    monkeypatch.setattr("reviewbot.github_source._reject_private_address", lambda host: None)
    transport = FakeTransport()
    transport.add(
        "/chat/completions",
        {
            "model": "deepseek-chat",
            "choices": [{"message": {"content": '{"summary": "ok", "suggestions": []}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    settings = Settings(
        db_path=str(tmp_path / "auth.db"),
        llm_api_key="test-key",
        llm_max_retries=0,
        api_key=api_key,
        rate_limit_per_minute=1000,
    )
    container = Container(settings=settings, transport=transport.as_transport())
    container.redis = FakeRedis()
    container.cache.client = container.redis
    return TestClient(build_app(container))


def _call(client: TestClient, method: str, path: str, body, headers=None):
    kwargs = {"headers": headers or {}}
    if body is None:
        kwargs["content"] = b"x = 1\n"
    else:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
async def test_protected_endpoints_reject_missing_key(tmp_path, monkeypatch, method, path, body):
    with _client(tmp_path, monkeypatch, api_key=API_KEY) as client:
        response = _call(client, method, path, body)
    assert response.status_code == 401, f"{method} {path} 未带 key 时应为 401，实际 {response.status_code}"


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
async def test_protected_endpoints_reject_wrong_key(tmp_path, monkeypatch, method, path, body):
    with _client(tmp_path, monkeypatch, api_key=API_KEY) as client:
        response = _call(client, method, path, body, headers={API_KEY_HEADER: "wrong"})
    assert response.status_code == 403, f"{method} {path} 错 key 时应为 403，实际 {response.status_code}"


async def test_correct_key_is_accepted(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, api_key=API_KEY) as client:
        response = client.post("/v1/reviews", json={"code": "x = 1\n"}, headers={API_KEY_HEADER: API_KEY})
    assert response.status_code == 202


async def test_stats_works_with_correct_key(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, api_key=API_KEY) as client:
        response = client.get("/v1/stats", headers={API_KEY_HEADER: API_KEY})
    assert response.status_code == 200


@pytest.mark.parametrize(("method", "path", "body"), PUBLIC)
async def test_health_endpoints_stay_public(tmp_path, monkeypatch, method, path, body):
    """探活不该需要凭据：否则编排系统必须先拿到密钥才能判断进程是否活着。"""
    with _client(tmp_path, monkeypatch, api_key=API_KEY) as client:
        response = _call(client, method, path, body)
    assert response.status_code == 200


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
async def test_no_key_configured_means_no_auth(tmp_path, monkeypatch, method, path, body):
    """留空即关闭鉴权（本机模式）—— 业务端点不该因为没配 key 就全部 401。"""
    with _client(tmp_path, monkeypatch, api_key="") as client:
        response = _call(client, method, path, body)
    assert response.status_code not in (401, 403)


async def test_401_takes_precedence_over_404(tmp_path, monkeypatch):
    """未认证时不应泄露"该任务是否存在"。"""
    with _client(tmp_path, monkeypatch, api_key=API_KEY) as client:
        response = client.get("/v1/tasks/definitely-not-exist")
    assert response.status_code == 401


# ---------------------------------------------------------------- 配置层


async def test_exposed_without_auth_detects_dangerous_combo():
    assert Settings(host="0.0.0.0", api_key="").exposed_without_auth is True
    assert Settings(host="0.0.0.0", api_key="k").exposed_without_auth is False
    assert Settings(host="127.0.0.1", api_key="").exposed_without_auth is False
    assert Settings(host="localhost", api_key="").exposed_without_auth is False


async def test_default_host_is_loopback():
    """默认必须是回环：这是"不设 key 也不至于被打"的第一道防线。"""
    assert Settings().host == "127.0.0.1"
