"""语义指纹（缓存 key 的基础）与启发式规则测试。"""

from __future__ import annotations

import ast

import pytest

from reviewbot.analysis import normalize_source
from reviewbot.analysis.analyzer import analyze_python
from reviewbot.analysis.heuristics import run_heuristics

# ---------------------------------------------------------------- 归一化


def test_comments_do_not_change_fingerprint():
    """v1 只做 strip()，加一行注释就会导致缓存未命中。"""
    a = "x = 1\n"
    b = "# 新增注释\nx = 1  # 行尾注释\n"
    assert normalize_source(a) == normalize_source(b)


def test_formatting_does_not_change_fingerprint():
    a = "def f(x):\n    return x + 1\n"
    b = "def f(x):\n\n    return x + 1\n"
    c = "def f( x ):\n    return (x + 1)\n"
    assert normalize_source(a) == normalize_source(b)
    assert normalize_source(a) == normalize_source(c)


def test_crlf_and_spaces_are_irrelevant():
    unix = "x = 1\ny = 2\n"
    windows = "x = 1\r\ny  =  2\r\n"
    assert normalize_source(unix) == normalize_source(windows)


def test_variable_rename_does_change_fingerprint():
    """刻意不消除命名差异：a 与 b 是不同程序，归一到同一 key 会命错结果。"""
    assert normalize_source("a = 1\n") != normalize_source("b = 1\n")


def test_literal_change_does_change_fingerprint():
    assert normalize_source("x = 1\n") != normalize_source("x = 2\n")


def test_invalid_source_has_no_fingerprint():
    assert normalize_source("def f(:\n") is None


# ---------------------------------------------------------------- 启发式


def _codes(source: str) -> set[str]:
    return {f.code for f in run_heuristics(ast.parse(source))}


def test_bare_except_detected():
    assert "BARE_EXCEPT" in _codes("try:\n    pass\nexcept:\n    pass\n")


def test_typed_except_not_flagged_as_bare():
    assert "BARE_EXCEPT" not in _codes("try:\n    pass\nexcept ValueError:\n    pass\n")


def test_mutable_default_detected():
    assert "MUTABLE_DEFAULT" in _codes("def f(x=[]):\n    return x\n")
    assert "MUTABLE_DEFAULT" in _codes("def f(x={}):\n    return x\n")


def test_immutable_default_not_flagged():
    assert "MUTABLE_DEFAULT" not in _codes("def f(x=1, y=None):\n    return x\n")


def test_none_comparison_detected():
    assert "NONE_COMPARISON" in _codes("if x == None:\n    pass\n")
    assert "NONE_COMPARISON" not in _codes("if x is None:\n    pass\n")


def test_unreachable_code_after_return_detected():
    findings = run_heuristics(ast.parse("def f():\n    return 1\n    print('never')\n"))
    unreachable = [f for f in findings if f.code == "UNREACHABLE_CODE"]
    assert len(unreachable) == 1
    assert unreachable[0].line == 3


def test_unreachable_not_reported_across_branches():
    """if/else 各自 return 不是不可达代码，避免误报。"""
    source = "def f(x):\n    if x:\n        return 1\n    else:\n        return 2\n"
    assert "UNREACHABLE_CODE" not in _codes(source)


def test_broad_except_detected():
    assert "BROAD_EXCEPT" in _codes("try:\n    pass\nexcept Exception:\n    pass\n")


def test_findings_sorted_by_line():
    source = (
        "def f(x=[]):\n"
        "    try:\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
    )
    findings = run_heuristics(ast.parse(source))
    lines = [f.line for f in findings if f.line]
    assert lines == sorted(lines)


# ---------------------------------------------------------------- 编排


def test_analyze_python_reports_parse_error_without_raising():
    result = analyze_python("def f(:\n")
    assert result.parse_error is not None
    assert result.findings == []
    assert result.structure.total_lines == 0


def test_analyze_python_happy_path():
    result = analyze_python("def f(n):\n    for i in range(n):\n        print(i)\n")
    assert result.parse_error is None
    assert result.structure.max_loop_nesting == 1
    assert result.recursion.has_recursion is False


def test_empty_source_analyzes_cleanly():
    result = analyze_python("")
    assert result.parse_error is None
    assert result.structure.max_loop_nesting == 0


@pytest.mark.parametrize("source", ["x = 1", "class A: pass", "import os"])
def test_simple_sources_have_no_findings(source):
    assert analyze_python(source).findings == []
