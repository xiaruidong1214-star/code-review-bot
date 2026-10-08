"""AST 静态分析。

**这里有一条重要的诚实边界**：本模块只做*结构统计*与*图上的环检测*，
不做时间复杂度分析，也不推导渐近复杂度。

模块分工：
* :mod:`reviewbot.analysis.metrics`    —— 数得清的客观计数（循环嵌套、行数、注释率）
* :mod:`reviewbot.analysis.callgraph`  —— 图论问题（直接递归 / 互递归）
* :mod:`reviewbot.analysis.heuristics` —— 有明确规则的代码味道
* :mod:`reviewbot.analysis.analyzer`   —— 编排以上三者

v1 曾经用「循环嵌套层数 → O(n^k)」做映射，那是错的，反例：
``for i in range(n): for j in range(3)`` 实际是 O(n)，被误判成 O(n^2)；
而两个并列的 ``range(n)`` 循环是 O(n^2)，被误判成 O(n)。
"""

from __future__ import annotations

from reviewbot.analysis.analyzer import analyze_python, normalize_source
from reviewbot.analysis.callgraph import RecursionReport, build_call_graph, detect_recursion

__all__ = [
    "RecursionReport",
    "analyze_python",
    "build_call_graph",
    "detect_recursion",
    "normalize_source",
]
