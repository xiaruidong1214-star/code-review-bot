"""集中配置：全部来自环境变量（前缀 ``CRB_``），可用 ``.env`` 覆盖。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 应用 ----
    app_name: str = "code-review-bot"
    env: str = "dev"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = True

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = Field(default=3600, ge=1)
    lock_ttl_seconds: int = Field(default=45, ge=1)
    singleflight_wait_seconds: float = Field(default=30.0, gt=0)
    result_ttl_seconds: int = Field(default=1800, ge=1)
    rate_limit_per_minute: int = Field(default=60, ge=1)

    # ---- 存储 ----
    db_path: str = "data/reviews.db"
    max_stored_code_chars: int = Field(default=20000, ge=100)

    # ---- LLM ----
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_input_chars: int = Field(default=20000, ge=100)

    # ---- 输入限制 ----
    max_code_bytes: int = Field(default=1024 * 1024, ge=1024)
    max_stream_bytes: int = Field(default=1024 * 1024, ge=1024)

    # ---- GitHub 抓取 ----
    github_token: str = ""
    github_allowed_hosts: tuple[str, ...] = ("github.com", "raw.githubusercontent.com")
    github_max_file_bytes: int = Field(default=256 * 1024, ge=1024)
    github_timeout_seconds: float = Field(default=15.0, gt=0)

    # ---- Celery（可选）----
    celery_broker_url: str = ""
    celery_result_backend: str = ""

    @field_validator("github_allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(h.strip() for h in v.split(",") if h.strip())
        return v

    @property
    def celery_enabled(self) -> bool:
        return bool(self.celery_broker_url)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例，避免每次请求重复解析环境变量。"""
    return Settings()
