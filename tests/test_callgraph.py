"""递归检测测试。

v1 只比较 ``child.func.id == node.name``，因此**互递归一个都检测不到**。
这里用 A→B→A、三节点环、以及非递归的相互调用（不成环）分别验证。
"""

from __future__ import annotations

import ast

from reviewbot.analysis.callgraph import build_call_graph, detect_recursion


def _report(source: str):
    return detect_recursion(build_call_graph(ast.parse(source)))


def test_direct_recursion_detected():
    report = _report("def fact(n):\n    return 1 if n <= 1 else n * fact(n - 1)\n")
    assert report.has_recursion
    assert report.direct_recursive == ["fact"]


def test_mutual_recursion_detected():
    """v1 的核心盲区。"""
    source = "def a(n):\n    return b(n - 1)\n\ndef b(n):\n    return a(n - 1)\n"
    report = _report(source)
    assert report.has_recursion
    assert report.mutually_recursive_groups == [["a", "b"]]
    assert report.direct_recursive == []


def test_three_node_cycle_detected():
    source = (
        "def a(x):\n    return b(x)\n\n"
        "def b(x):\n    return c(x)\n\n"
        "def c(x):\n    return a(x)\n"
    )
    report = _report(source)
    assert report.mutually_recursive_groups == [["a", "b", "c"]]


def test_acyclic_mutual_calls_are_not_recursion():
    """a 调 b 但 b 不回头，这不是递归 —— 避免误报。"""
    source = "def a():\n    return b()\n\ndef b():\n    return 1\n"
    report = _report(source)
    assert not report.has_recursion


def test_async_function_recursion_detected():
    source = "async def walk(n):\n    return await walk(n - 1)\n"
    assert _report(source).direct_recursive == ["walk"]


def test_method_recursion_qualified_name():
    source = "class A:\n    def m(self):\n        return self.m()\n"
    report = _report(source)
    assert report.direct_recursive == ["A.m"]


def test_nested_function_scope_is_isolated():
    """内层函数名不应污染外层调用集合。"""
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        return inner()\n"
        "    return inner()\n"
    )
    graph = build_call_graph(ast.parse(source))
    assert graph["outer"] == {"inner"}
    assert graph["outer.inner"] == {"inner"}


def test_callback_in_key_argument_is_traced():
    source = "def cmp(x):\n    return x\n\ndef run(xs):\n    return sorted(xs, key=cmp)\n"
    graph = build_call_graph(ast.parse(source))
    assert "cmp" in graph["run"]


def test_builtin_calls_ignored():
    """print 之类不在图内，不能被当成环的一部分。"""
    source = "def a():\n    print('x')\n    return a()\n"
    report = _report(source)
    assert report.direct_recursive == ["a"]
    assert report.mutually_recursive_groups == []


def test_empty_source():
    report = _report("")
    assert not report.has_recursion
