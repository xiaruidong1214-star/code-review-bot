"""HTTP 层测试（用真实 ASGI 应用 + 替身基础设施，无需 Redis / 外网）。"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from reviewbot.api import build_app
from reviewbot.bootstrap import Container
from reviewbot.settings import Settings
from tests.conftest import FakeRedis

CODE = "def f(n):\n    for i in range(n):\n        if n == None:\n            pass\n"
LLM_BODY = {
    "model": "deepseek-chat",
    "choices": [
        {"message": {"content": json.dumps({"summary": "还行", "suggestions": ["去掉 == None"]})}}
    ],
    "usage": {"prompt_tokens": 80, "completion_tokens": 15},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tests.conftest import FakeTransport

    monkeypatch.setattr("reviewbot.github_source._reject_private_address", lambda host: None)

    transport = FakeTransport()
    transport.add("/chat/completions", LLM_BODY)

    settings = Settings(
        db_path=str(tmp_path / "api.db"),
        llm_api_key="test-key",
        llm_max_retries=0,
        cache_ttl_seconds=60,
        result_ttl_seconds=60,
        rate_limit_per_minute=1000,
        singleflight_wait_seconds=2.0,
    )
    container = Container(settings=settings, transport=transport.as_transport())
    container.redis = FakeRedis()  # 换掉真实 Redis 客户端
    container.cache.client = container.redis

    with TestClient(build_app(container)) as test_client:
        yield test_client


def _wait_for_success(client: TestClient, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/v1/tasks/{task_id}")
        assert response.status_code == 200
        body = response.json()
        if body["state"] in {"SUCCESS", "FAILED"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内结束")


# ---------------------------------------------------------------- 基础


def test_health_reports_dependencies(client):
    body = client.get("/v1/health").json()
    assert body["status"] == "ok"
    assert body["redis"] is True
    assert body["database"] is True
    assert body["llm_configured"] is True


def test_livez(client):
    assert client.get("/v1/livez").status_code == 200


def test_response_headers_carry_request_id(client):
    response = client.get("/v1/stats")
    assert response.headers["x-request-id"]
    assert float(response.headers["x-response-time-ms"]) >= 0


# ---------------------------------------------------------------- 提交与查询


def test_submit_and_poll_to_completion(client):
    response = client.post("/v1/reviews", json={"code": CODE})
    assert response.status_code == 202
    body = response.json()
    assert body["source_type"] == "code"
    assert body["cache_hit"] is False

    final = _wait_for_success(client, body["task_id"])
    assert final["state"] == "SUCCESS"
    assert final["progress"] == 100
    assert final["analysis"]["structure"]["loop_count"] == 1
    assert final["analysis"]["findings"][0]["code"] == "NONE_COMPARISON"
    assert final["llm"]["suggestions"] == ["去掉 == None"]
    assert final["llm"]["degraded"] is False


def test_semantic_cache_hit_reported(client):
    first = client.post("/v1/reviews", json={"code": CODE}).json()
    _wait_for_success(client, first["task_id"])

    reformatted = "# 注释不影响\n" + CODE + "\n"
    second = client.post("/v1/reviews", json={"code": reformatted}).json()
    assert second["cache_hit"] is True
    assert second["status"] == "SUCCESS"

    final = client.get(f"/v1/tasks/{second['task_id']}").json()
    assert final["llm"]["summary"] == "还行"


def test_unknown_task_returns_404(client):
    assert client.get("/v1/tasks/does-not-exist").status_code == 404


def test_request_requires_exactly_one_source(client):
    assert client.post("/v1/reviews", json={}).status_code == 422
    assert client.post("/v1/reviews", json={"code": "x=1", "github_url": "https://github.com/a/b/blob/m/c.py"}).status_code == 422


def test_non_github_host_accepted_then_fails_async(client):
    """白名单校验发生在抓取阶段，因此提交返回 202，随后任务如实变为 FAILED。

    这正是不该做的反面：v1 遇到不支持的来源会返回 ``source_type="github"``
    并可能悄悄跳过，让调用方以为成功了。
    """
    url = "https://gitlab.example.com/a/b/blob/main/c.py"
    response = client.post("/v1/reviews", json={"github_url": url})
    assert response.status_code == 202

    final = _wait_for_success(client, response.json()["task_id"])
    assert final["state"] == "FAILED"
    assert "GitHub" in (final["error"] or "")


def test_github_url_on_allowlist_reaches_fetcher(client, monkeypatch):
    """白名单内的链接会真的去抓取（此处用替身返回源码）。"""
    from reviewbot.github_source import GitHubFetcher

    async def fake_fetch(self, url: str) -> str:
        return CODE

    monkeypatch.setattr(GitHubFetcher, "fetch", fake_fetch)
    url = "https://github.com/a/b/blob/main/c.py"
    response = client.post("/v1/reviews", json={"github_url": url})
    assert response.status_code == 202

    final = _wait_for_success(client, response.json()["task_id"])
    assert final["state"] == "SUCCESS"
    assert final["analysis"]["structure"]["loop_count"] == 1


def test_parse_error_is_not_failure(client):
    response = client.post("/v1/reviews", json={"code": "def f(:\n"})
    final = _wait_for_success(client, response.json()["task_id"])
    assert final["state"] == "SUCCESS"
    assert final["analysis"]["parse_error"] is not None
    assert final["llm"]["degraded"] is True


# ---------------------------------------------------------------- 流式上传


def test_stream_upload_accepts_body(client):
    response = client.post("/v1/reviews/stream", content=CODE.encode("utf-8"))
    assert response.status_code == 202
    final = _wait_for_success(client, response.json()["task_id"])
    assert final["state"] == "SUCCESS"


def test_stream_upload_rejects_oversize(client, tmp_path):
    """真正的流式读取会边收边判上限，而不是先整体读进内存。"""
    big = b"x = 1\n" * 400_000
    response = client.post("/v1/reviews/stream", content=big)
    assert response.status_code == 413


def test_stream_upload_rejects_empty(client):
    assert client.post("/v1/reviews/stream", content=b"").status_code == 400


def test_stream_upload_rejects_non_utf8(client):
    assert client.post("/v1/reviews/stream", content=b"\xff\xfe\x00").status_code == 400


# ---------------------------------------------------------------- 其他端点


def test_history_returns_finished_items(client):
    task_id = client.post("/v1/reviews", json={"code": CODE}).json()["task_id"]
    _wait_for_success(client, task_id)

    items = client.get("/v1/history").json()
    assert items and items[0]["task_id"] == task_id
    assert items[0]["max_loop_nesting"] == 1
    assert len(items[0]["code_preview"]) <= 100


def test_history_limit_is_clamped(client):
    assert client.get("/v1/history", params={"limit": 10_000}).status_code == 200


def test_stats_counts_reviews(client):
    task_id = client.post("/v1/reviews", json={"code": CODE}).json()["task_id"]
    _wait_for_success(client, task_id)

    stats = client.get("/v1/stats").json()
    assert stats["total_reviews"] == 1
    assert stats["failed_reviews"] == 0


def test_sse_events_endpoint_exists(client):
    task_id = client.post("/v1/reviews", json={"code": CODE}).json()["task_id"]
    _wait_for_success(client, task_id)
    with client.stream("GET", f"/v1/tasks/{task_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")


def test_sse_unknown_task_404(client):
    assert client.get("/v1/tasks/nope/events").status_code == 404


def test_rate_limit_returns_429(tmp_path, monkeypatch):
    from reviewbot import api as api_module
    from tests.conftest import FakeTransport

    monkeypatch.setattr("reviewbot.github_source._reject_private_address", lambda host: None)
    # 限流桶是进程级状态，先清空以免受其他用例影响
    api_module._rate_buckets.clear()

    transport = FakeTransport()
    transport.add("/chat/completions", LLM_BODY)
    settings = Settings(
        db_path=str(tmp_path / "rl.db"),
        llm_api_key="k",
        rate_limit_per_minute=2,
        llm_max_retries=0,
    )
    container = Container(settings=settings, transport=transport.as_transport())
    container.redis = FakeRedis()
    container.cache.client = container.redis

    with TestClient(build_app(container)) as c:
        assert c.post("/v1/reviews", json={"code": "a = 1"}).status_code == 202
        assert c.post("/v1/reviews", json={"code": "b = 2"}).status_code == 202
        assert c.post("/v1/reviews", json={"code": "c = 3"}).status_code == 429
