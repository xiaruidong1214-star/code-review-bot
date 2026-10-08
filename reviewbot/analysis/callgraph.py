"""调用图构建与递归检测。

v1 只比较 ``child.func.id == node.name``，也就是**只认直接自递归**，
A→B→A 这种互递归完全检测不到。这里改为先建调用图，再用 Tarjan 求强连通分量，
从而同时覆盖直接递归、间接递归与互递归。

局限（写在代码里而不是烂在注释里）：这是**静态**图。通过 ``getattr``、
装饰器重写、``eval`` 产生的调用边看不见；跨文件调用也不在范围内。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# 这些内建函数把参数当回调用，回调名也要算作被调用者
_HIGHER_ORDER_BUILTINS = {"map", "filter", "sorted", "min", "max", "reduce"}


@dataclass(slots=True)
class RecursionReport:
    has_recursion: bool = False
    direct_recursive: list[str] = field(default_factory=list)
    mutually_recursive_groups: list[list[str]] = field(default_factory=list)


def _is_callback_slot(func: ast.expr, kw: ast.keyword | None) -> bool:
    """判断某个实参位置是否属于「要传函数进去」的槽位。"""
    if isinstance(func, ast.Name) and func.id in _HIGHER_ORDER_BUILTINS:
        return True
    if isinstance(func, ast.Attribute) and func.attr in {"sort", "apply_async", "delay"}:
        return True
    return kw is not None and kw.arg in {
        "key",
        "default",
        "func",
        "callback",
        "errback",
        "link",
        "target",
        "factory",
    }


def build_call_graph(tree: ast.AST) -> dict[str, set[str]]:
    """返回 ``限定函数名 -> 它调用的函数名集合``。

    用「作用域栈」生成限定名：``Outer.inner``、``Class.method``，
    避免嵌套作用域互相污染。只有*定义在模块文件里的函数*才会成为图节点，
    因此内建与第三方调用天然被忽略。
    """
    graph: dict[str, set[str]] = {}
    scope: list[str] = []
    class_stack: list[str] = []

    def full_name(name: str) -> str:
        return ".".join([*scope, name]) if scope else name

    def visit(node: ast.AST, current: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = full_name(child.name)
                graph.setdefault(qualified, set())
                scope.append(child.name)
                visit(child, qualified)
                scope.pop()
                continue

            if isinstance(child, ast.ClassDef):
                scope.append(child.name)
                class_stack.append(".".join(scope))
                visit(child, current)
                class_stack.pop()
                scope.pop()
                continue

            if current is not None and isinstance(child, ast.Call):
                graph[current].update(
                    _resolve_call_targets(child, class_stack[-1] if class_stack else None)
                )

            visit(child, current)

    visit(tree, None)
    return graph


def _resolve_call_targets(call: ast.Call, current_class: str | None = None) -> set[str]:
    """解析一次调用可能指向的被调用者名字。

    ``self.m()`` 这类方法调用必须解析成 ``Class.m``，
    否则方法级递归永远检测不到 —— 这正是 v1 的盲区之一。
    """
    targets: set[str] = set()
    func = call.func

    if isinstance(func, ast.Name):
        targets.add(func.id)
    elif isinstance(func, ast.Attribute):
        targets.add(func.attr)
        # self.m() / cls.m() / obj.m() → 归属当前类
        if current_class is not None and isinstance(func.value, ast.Name):
            if func.value.id in {"self", "cls"} or func.value.id[:1].islower():
                targets.add(f"{current_class}.{func.attr}")

    # 回调位置：sorted(xs, key=f) / map(f, xs) / threading.Thread(target=f)
    for arg in call.args:
        if isinstance(arg, ast.Name) and _is_callback_slot(func, None):
            targets.add(arg.id)
    for kw in call.keywords:
        if isinstance(kw.value, ast.Name) and _is_callback_slot(func, kw):
            targets.add(kw.value.id)
    if current_class is not None:
        for kw in call.keywords:
            if isinstance(kw.value, ast.Attribute) and isinstance(kw.value.value, ast.Name):
                if kw.value.value.id in {"self", "cls"}:
                    targets.add(f"{current_class}.{kw.value.attr}")

    return targets


def detect_recursion(graph: dict[str, set[str]]) -> RecursionReport:
    """在调用图上用 Tarjan 求强连通分量，识别自递归与互递归。

    复杂度 O(V+E)。边指向图外节点（内建、第三方、未定义函数）时被忽略，
    避免把 ``print`` 之类误当成环的一部分。
    """
    index_of: dict[str, int] = {}
    low_of: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal counter
        index_of[node] = low_of[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)

        for succ in graph.get(node, ()):
            if succ not in graph:
                continue
            if succ not in index_of:
                strongconnect(succ)
                low_of[node] = min(low_of[node], low_of[succ])
            elif succ in on_stack:
                low_of[node] = min(low_of[node], index_of[succ])

        if low_of[node] == index_of[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            components.append(sorted(component))

    for node in list(graph):
        if node not in index_of:
            strongconnect(node)

    direct: list[str] = []
    mutual: list[list[str]] = []
    for component in components:
        if len(component) == 1:
            only = component[0]
            if only in graph.get(only, set()):
                direct.append(only)
        elif any(set(graph.get(n, set())) & set(component) for n in component):
            mutual.append(sorted(component))

    return RecursionReport(
        has_recursion=bool(direct or mutual),
        direct_recursive=sorted(direct),
        mutually_recursive_groups=mutual,
    )
