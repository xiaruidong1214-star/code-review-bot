"""对外 API 的数据契约（Pydantic v2）。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, model_validator


class TaskState(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class SourceType(StrEnum):
    CODE = "code"
    CODE_STREAM = "code_stream"
    GITHUB = "github"


class ReviewRequest(BaseModel):
    code: str | None = Field(default=None, description="直接提交的代码文本")
    github_url: HttpUrl | None = Field(default=None, description="GitHub 文件链接")
    language: str = Field(default="python", max_length=32)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ReviewRequest:
        if bool(self.code) == bool(self.github_url):
            raise ValueError("必须且只能提供 code 或 github_url 其中之一")
        return self


class ReviewAccepted(BaseModel):
    task_id: str
    status: TaskState = TaskState.PENDING
    source_type: SourceType
    message: str = "任务已提交，可通过 /v1/tasks/{task_id} 查询进度"
    cache_hit: bool = False


class StructureMetrics(BaseModel):
    """纯结构统计。**不是** 时间复杂度分析，命名刻意避免 ``complexity``。"""

    max_loop_nesting: int
    loop_count: int
    function_count: int
    class_count: int
    total_lines: int
    code_lines: int
    comment_lines: int
    docstring_lines: int
    blank_lines: int
    comment_ratio: float = Field(description="注释行 / 有效代码行，来自 tokenize，不含字符串内的 #")

    # 诚实声明：结构统计无法推导渐进复杂度
    approximation_notice: str = (
        "以上为静态结构统计，不能推导时间复杂度。"
        "例如 for i in range(n): for j in range(3) 的实际复杂度是 O(n)，"
        "而两个并列的 range(n) 循环是 O(n^2)。"
    )


class RecursionReport(BaseModel):
    has_recursion: bool
    direct_recursive: list[str] = Field(default_factory=list)
    mutually_recursive_groups: list[list[str]] = Field(
        default_factory=list, description="互递归强连通分量（A→B→A 这类环）"
    )


class Finding(BaseModel):
    code: str
    severity: str
    line: int | None = None
    message: str


class AnalysisResult(BaseModel):
    language: str
    structure: StructureMetrics
    recursion: RecursionReport
    findings: list[Finding] = Field(default_factory=list)
    parse_error: str | None = None


class LLMReview(BaseModel):
    summary: str
    suggestions: list[str] = Field(default_factory=list)
    degraded: bool = Field(
        default=False,
        description="true 表示 LLM 不可用，此处为降级结果。客户端与统计必须区别对待。",
    )
    error: str | None = None


class TaskResult(BaseModel):
    task_id: str
    state: TaskState
    progress: int = 0
    step: str = ""
    analysis: AnalysisResult | None = None
    llm: LLMReview | None = None
    error: str | None = None
    cache_hit: bool = False


class HistoryItem(BaseModel):
    task_id: str
    language: str
    max_loop_nesting: int | None = None
    code_preview: str
    created_at: str


class Stats(BaseModel):
    total_reviews: int
    failed_reviews: int
    cache_hits: int
    llm_degraded: int


class HealthResponse(BaseModel):
    status: str
    version: str
    redis: bool
    database: bool
    llm_configured: bool
    detail: dict[str, Any] = Field(default_factory=dict)
