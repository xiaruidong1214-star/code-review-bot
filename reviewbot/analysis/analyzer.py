"""分析编排层：把结构度量、调用图、启发式拼装成一个结果对象。

对外只暴露两个入口：
* :func:`normalize_source` —— 生成用于缓存的语义指纹（AST 级别归一化）；
* :func:`analyze_python`   —— 产出完整的静态分析结果。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from reviewbot.analysis.callgraph import RecursionReport, build_call_graph, detect_recursion
from reviewbot.analysis.heuristics import Finding, run_heuristics
from reviewbot.analysis.metrics import StructureMetrics, collect_structure


class ParseError(ValueError):
    """代码无法解析为 Python 语法树。"""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


@dataclass(slots=True)
class AnalyzeResult:
    language: str
    structure: StructureMetrics
    recursion: RecursionReport
    findings: list[Finding] = field(default_factory=list)
    parse_error: str | None = None


def normalize_source(source: str) -> str | None:
    """返回与注释、空白、缩进风格无关的 AST 指纹；无法解析时返回 ``None``。

    这是对 v1 的关键修正。v1 的缓存 key 是 ``sha256(language + code.strip())``，
    改一个空格、换一种换行符、加一行注释都会导致未命中，
    所以「重复请求降低 95%」这个宣称在语义上站不住。

    **刻意不做的事情**：不消除变量名差异。``a`` 与 ``b`` 是语义不同的程序，
    把它们归一到同一个 key 会命中错误的结果，那是比低命中率更严重的问题。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def analyze_python(source: str) -> AnalyzeResult:
    """对一段 Python 源码做静态分析。解析失败不抛异常，而是如实返回错误。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return AnalyzeResult(
            language="python",
            structure=StructureMetrics(),
            recursion=RecursionReport(),
            findings=[],
            parse_error=f"{exc.msg} (line {exc.lineno})",
        )

    return AnalyzeResult(
        language="python",
        structure=collect_structure(tree, source),
        recursion=detect_recursion(build_call_graph(tree)),
        findings=run_heuristics(tree),
        parse_error=None,
    )
