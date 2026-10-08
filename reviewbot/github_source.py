"""GitHub 源码抓取。

v1 这里是个占位符：

.. code-block:: python

    code_content = code or "# GitHub 代码获取待实现"

但它对外仍然返回 ``source_type="github"``，也就是**功能不存在却表现为成功**。
现在要么真的抓下来，要么明确报错。

同时补上 SSRF 防护：不允许任意主机、禁止内网地址、限制体积与重定向目标。
"""

from __future__ import annotations

import base64
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from reviewbot.logging_setup import get_logger

logger = get_logger(__name__)

_API = "https://api.github.com"


class GitHubFetchError(ValueError):
    """抓取失败：URL 非法、被策略拒绝、或上游返回错误。"""


@dataclass(slots=True)
class GitHubTarget:
    owner: str
    repo: str
    path: str
    ref: str | None = None


def parse_github_url(url: str, allowed_hosts: tuple[str, ...]) -> GitHubTarget:
    """严格解析 GitHub 文件链接，只接受 ``/{owner}/{repo}/blob|raw/{ref}/{path}``。"""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise GitHubFetchError("只允许 https 链接")

    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise GitHubFetchError(f"主机 {host!r} 不在白名单内: {allowed_hosts}")
    _reject_private_address(host)

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 5 or parts[2] not in {"blob", "raw"}:
        raise GitHubFetchError("链接格式应为 https://github.com/{owner}/{repo}/blob/{ref}/{path}")

    owner, repo, _, ref = parts[0], parts[1], parts[2], parts[3]
    path = "/".join(parts[4:])
    return GitHubTarget(owner=owner, repo=repo, path=path, ref=ref)


def _reject_private_address(host: str) -> None:
    """把域名解析成 IP，拒绝环回 / 私网 / 链路本地等地址。

    这是 SSRF 防护的核心：即使域名看着像 github.com，
    也必须确认它没有通过 DNS 指向内网。
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise GitHubFetchError(f"无法解析主机 {host!r}") from exc

    for info in infos:
        address = info[4][0]
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise GitHubFetchError(f"拒绝访问非公网地址 {address}")


class GitHubFetcher:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        token: str = "",
        max_bytes: int = 256 * 1024,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.max_bytes = max_bytes
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # follow_redirects=False：重定向目标必须重新校验，不能让客户端自动跟
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=False, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> str:
        target = parse_github_url(url, self.allowed_hosts)
        api_url = f"{_API}/repos/{target.owner}/{target.repo}/contents/{target.path}"
        params = {"ref": target.ref} if target.ref else None

        response = await self._client.get(api_url, params=params)
        if response.status_code == 404:
            raise GitHubFetchError("文件不存在，或仓库为私有（未配置 CRB_GITHUB_TOKEN）")
        if response.status_code == 403:
            raise GitHubFetchError("GitHub API 限流，请配置 CRB_GITHUB_TOKEN 后重试")
        response.raise_for_status()

        body = response.json()
        if body.get("type") != "file":
            raise GitHubFetchError("链接指向的不是文件")
        if body.get("size", 0) > self.max_bytes:
            raise GitHubFetchError(f"文件超过 {self.max_bytes} 字节上限")

        encoded = body.get("content", "")
        try:
            raw = base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubFetchError("文件不是可解码的 UTF-8 文本") from exc

        logger.info("github_fetch_ok", owner=target.owner, repo=target.repo, path=target.path, size=len(raw))
        return raw
