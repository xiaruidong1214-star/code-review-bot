"""鉴权。

**为什么加这个**：本服务背后挂着按量计费的 LLM，并且能读写 SQLite、能对外发起
GitHub 抓取。如果它监听在可达地址上又没有任何凭据校验，任何能碰到该端口的人
都可以免费用掉你的额度、写满你的磁盘。早期版本就是这样（默认 `0.0.0.0` 且零鉴权），
这属于会真实造成损失的设计缺口。

设计取舍：

* **不设 `CRB_API_KEY` 时不做校验** —— 保留本机自用的便利；
  但默认监听已改为回环地址（`CRB_HOST=127.0.0.1`），两者叠加才安全。
* 如果监听了非回环地址又没设 key，启动时会打印**醒目警告**（见 `warn_if_exposed`），
  但**不阻止启动** —— 有些部署把鉴权放在反向代理层（nginx / API 网关），
  强行拒绝会挡掉这种合法用法。
* 健康检查端点（`/v1/health`、`/v1/livez`）**刻意不校验**：编排系统探活不该需要凭据。
* 比较用 :func:`hmac.compare_digest` 而不是 `==`，避免时序侧信道。
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from reviewbot.logging_setup import get_logger

logger = get_logger(__name__)

API_KEY_HEADER = "X-API-Key"


def require_api_key(request: Request) -> None:
    """FastAPI 依赖：校验 ``X-API-Key`` 请求头。

    未配置 key 时直接放行（本机模式）；配置了则必须匹配。
    """
    expected = request.app.state.container.settings.api_key
    if not expected:
        return

    provided = request.headers.get(API_KEY_HEADER, "")
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"缺少 {API_KEY_HEADER} 请求头",
        )
    # 常量时间比较：避免通过响应时间逐字节猜出 key
    if not hmac.compare_digest(provided, expected):
        logger.warning("auth_rejected", path=str(request.url.path), client=_client_of(request))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API Key 不正确")


def _client_of(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def warn_if_exposed(settings) -> None:
    """启动时检查「监听非回环 + 无鉴权」这一危险组合并告警。"""
    if settings.exposed_without_auth:
        logger.warning(
            "insecure_exposure",
            host=settings.host,
            hint="正在监听非回环地址且未设置 CRB_API_KEY：任何可达该端口的人都能使用"
            "你的 LLM 额度。请设置 CRB_API_KEY，或在反向代理层做鉴权。",
        )
