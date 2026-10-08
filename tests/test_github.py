"""GitHub 抓取与 SSRF 防护测试。

v1 这里是 ``code or "# GitHub 代码获取待实现"`` 的占位符，
却对外返回 ``source_type="github"``——功能不存在却表现为成功。
现在必须真的抓到代码，或者给出明确错误。
"""

from __future__ import annotations

import base64
import json

import pytest

from reviewbot.github_source import (
    GitHubFetcher,
    GitHubFetchError,
    _reject_private_address,
    parse_github_url,
)

ALLOWED = ("github.com", "raw.githubusercontent.com")


# ---------------------------------------------------------------- URL 解析


def test_valid_blob_url_parsed():
    target = parse_github_url("https://github.com/psf/requests/blob/main/src/requests/api.py", ALLOWED)
    assert (target.owner, target.repo, target.ref) == ("psf", "requests", "main")
    assert target.path == "src/requests/api.py"


def test_raw_style_url_parsed():
    target = parse_github_url("https://github.com/a/b/raw/v1.2/c.py", ALLOWED)
    assert target.ref == "v1.2"
    assert target.path == "c.py"


def test_http_rejected():
    with pytest.raises(GitHubFetchError, match="https"):
        parse_github_url("http://github.com/a/b/blob/main/c.py", ALLOWED)


def test_non_whitelisted_host_rejected():
    with pytest.raises(GitHubFetchError, match="白名单"):
        parse_github_url("https://evil.example.com/a/b/blob/main/c.py", ALLOWED)


def test_metadata_endpoint_rejected():
    """典型 SSRF 目标：云元数据服务。"""
    with pytest.raises(GitHubFetchError):
        parse_github_url("https://169.254.169.254/latest/meta-data/", ALLOWED)


def test_localhost_rejected():
    with pytest.raises(GitHubFetchError):
        parse_github_url("https://localhost/a/b/blob/main/c.py", ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/a/b",
        "https://github.com/a/b/tree/main/pkg",
        "https://github.com/a/blob/main",
    ],
)
def test_malformed_paths_rejected(url: str):
    with pytest.raises(GitHubFetchError):
        parse_github_url(url, ALLOWED)


def test_private_ip_literal_rejected():
    with pytest.raises(GitHubFetchError):
        _reject_private_address("127.0.0.1")


def test_public_host_resolves_ok():
    """真实解析一次公网域名，确认白名单里的主机不会被误杀。

    需要 DNS：若环境无外网则跳过，避免把网络问题误判为代码缺陷。
    """
    try:
        _reject_private_address("github.com")
    except GitHubFetchError as exc:  # pragma: no cover - 无 DNS 环境下走这里
        if "无法解析" in str(exc):
            pytest.skip("当前环境无 DNS，跳过公网解析校验")
        raise


# ---------------------------------------------------------------- 抓取行为


@pytest.fixture
def no_dns(monkeypatch):
    """用假 transport 时代替真实 DNS 解析，保持测试离线可跑。"""
    monkeypatch.setattr("reviewbot.github_source._reject_private_address", lambda host: None)


def _fetcher(transport, max_bytes: int = 1024) -> GitHubFetcher:
    return GitHubFetcher(allowed_hosts=ALLOWED, max_bytes=max_bytes, transport=transport)


async def test_fetch_decodes_base64_content(no_dns):
    import httpx

    body = {
        "type": "file",
        "size": 12,
        "content": base64.b64encode(b"x = 1\ny = 2\n").decode(),
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=body, request=req))
    fetcher = _fetcher(transport)
    try:
        content = await fetcher.fetch("https://github.com/a/b/blob/main/c.py")
    finally:
        await fetcher.aclose()
    assert content == "x = 1\ny = 2\n"


async def test_fetch_404_raises_clear_error(no_dns):
    import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(404, json={}, request=req))
    fetcher = _fetcher(transport)
    try:
        with pytest.raises(GitHubFetchError, match="不存在"):
            await fetcher.fetch("https://github.com/a/b/blob/main/c.py")
    finally:
        await fetcher.aclose()


async def test_fetch_403_rate_limit_hint(no_dns):
    import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(403, json={}, request=req))
    fetcher = _fetcher(transport)
    try:
        with pytest.raises(GitHubFetchError, match="限流"):
            await fetcher.fetch("https://github.com/a/b/blob/main/c.py")
    finally:
        await fetcher.aclose()


async def test_directory_link_rejected(no_dns):
    import httpx

    body = {"type": "dir", "size": 0, "content": ""}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=body, request=req))
    fetcher = _fetcher(transport)
    try:
        with pytest.raises(GitHubFetchError, match="不是文件"):
            await fetcher.fetch("https://github.com/a/b/blob/main/pkg")
    finally:
        await fetcher.aclose()


async def test_oversized_file_rejected(no_dns):
    import httpx

    body = {"type": "file", "size": 999999, "content": ""}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=body, request=req))
    fetcher = _fetcher(transport, max_bytes=1024)
    try:
        with pytest.raises(GitHubFetchError, match="上限"):
            await fetcher.fetch("https://github.com/a/b/blob/main/big.py")
    finally:
        await fetcher.aclose()


async def test_non_utf8_file_rejected(no_dns):
    import httpx

    body = {
        "type": "file",
        "size": 4,
        "content": base64.b64encode(b"\xff\xfe\x00\x01").decode(),
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=body, request=req))
    fetcher = _fetcher(transport)
    try:
        with pytest.raises(GitHubFetchError, match="UTF-8"):
            await fetcher.fetch("https://github.com/a/b/blob/main/bin.py")
    finally:
        await fetcher.aclose()


async def test_ref_is_forwarded_as_query_param(no_dns):
    import httpx

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        body = {"type": "file", "size": 2, "content": base64.b64encode(b"ok").decode()}
        return httpx.Response(200, json=body, request=request)

    fetcher = _fetcher(httpx.MockTransport(handler))
    try:
        await fetcher.fetch("https://github.com/a/b/blob/dev/path/c.py")
    finally:
        await fetcher.aclose()

    assert "ref=dev" in seen["url"]
    assert json.dumps(seen["url"])  # 保证 url 可序列化，便于日志
