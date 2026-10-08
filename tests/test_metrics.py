"""结构度量测试。

这些用例全部来自 v1 的真实缺陷：

* v1 的嵌套计数把并列循环也算成嵌套（``_enter_loop`` 无条件 +1），
  所以 ``for i: ... for j: ...`` 也会得到 depth=2；
* v1 用 ``line.startswith('#')`` 统计注释，字符串里的 ``#`` 与三引号都会算错。
"""

from __future__ import annotations

from reviewbot.analysis.metrics import count_lines


def _structure(source: str):
    from reviewbot.analysis.analyzer import analyze_python

    return analyze_python(source).structure


def test_sibling_loops_are_not_nested():
    source = "for i in range(3):\n    pass\nfor j in range(3):\n    pass\n"
    assert _structure(source).max_loop_nesting == 1


def test_broken_syntax_does_not_crash_metrics():
    """语法错误时结构统计归零，而不是抛异常打断整条审查流程。"""
    source = "for i in range(3):\n    for j in range(3)\n        pass\n"  # 缺冒号
    result = _structure(source)
    assert result.max_loop_nesting == 0
    assert result.total_lines == 0

    from reviewbot.analysis.analyzer import analyze_python

    assert analyze_python(source).parse_error is not None


def test_nested_loops_depth_two():
    source = "for i in range(n):\n    for j in range(n):\n        pass\n"
    assert _structure(source).max_loop_nesting == 2


def test_sequential_then_nested_takes_max_not_sum():
    source = "for a in x:\n    pass\nfor b in y:\n    for c in z:\n        pass\n"
    assert _structure(source).max_loop_nesting == 2


def test_while_inside_for_counts_as_nesting():
    source = "for i in x:\n    while i:\n        i -= 1\n"
    assert _structure(source).max_loop_nesting == 2


def test_constant_inner_loop_is_still_reported_as_nesting():
    """关键的诚实性用例：结构是 2 层嵌套，但复杂度其实是 O(n)。

    本服务只报结构，绝不宣称这是复杂度结论。
    """
    source = "for i in range(n):\n    for j in range(3):\n        pass\n"
    result = _structure(source)
    assert result.max_loop_nesting == 2
    assert "不能推导时间复杂度" in result.approximation_notice


def test_comment_hash_inside_string_is_not_a_comment():
    source = 'x = "# 这不是注释"\n'
    stats = count_lines(source)
    assert stats["comment_lines"] == 0
    assert stats["code_lines"] == 1


def test_trailing_comment_not_counted_as_comment_line():
    source = "x = 1  # 行尾注释\n"
    assert count_lines(source)["comment_lines"] == 0


def test_full_line_comment_counted():
    source = "# 说明\nx = 1\n"
    stats = count_lines(source)
    assert stats["comment_lines"] == 1
    assert stats["comment_ratio"] == 100.0


def test_docstring_is_not_counted_as_comment():
    source = 'def f():\n    """文档"""\n    return 1\n'
    stats = count_lines(source)
    assert stats["comment_lines"] == 0
    assert stats["docstring_lines"] == 1


def test_function_and_class_counts():
    source = "class A:\n    def m(self):\n        pass\n\ndef f():\n    pass\n"
    structure = _structure(source)
    assert structure.class_count == 1
    assert structure.function_count == 2


def test_tokenize_fallback_on_incomplete_source():
    """语法不完整时不应抛异常，而是退化统计。"""
    stats = count_lines("def f(:\n    pass\n")
    assert stats["total_lines"] == 2
