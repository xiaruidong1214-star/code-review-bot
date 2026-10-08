"""结构度量：只数得清的东西，不做任何复杂度推断。

两处 v1 遗留问题在这里被修掉：
1. 嵌套深度必须表达**嵌套**语义 —— 两个并列循环深度都是 1，不是 2；
2. 注释率改用 :mod:`tokenize`，不再用 ``line.startswith('#')``
   （那种写法会把字符串里的 ``#``、以及三引号文档字符串算错）。
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass

_LOOP_NODES = (ast.For, ast.AsyncFor, ast.While)

# 对外声明的诚实边界：结构统计推不出渐进复杂度
APPROXIMATION_NOTICE = (
    "以上为静态结构统计，不能推导时间复杂度。"
    "例如 for i in range(n): for j in range(3) 的实际复杂度是 O(n)，"
    "而两个并列的 range(n) 循环是 O(n^2)。"
)


@dataclass(slots=True)
class StructureMetrics:
    max_loop_nesting: int = 0
    loop_count: int = 0
    function_count: int = 0
    class_count: int = 0
    total_lines: int = 0
    code_lines: int = 0
    comment_lines: int = 0
    docstring_lines: int = 0
    blank_lines: int = 0
    comment_ratio: float = 0.0
    approximation_notice: str = APPROXIMATION_NOTICE


class _StructureVisitor(ast.NodeVisitor):
    """按「循环嵌套」定义累积深度。

    关键实现：进入循环时把 ``depth`` 传入子树；进入非循环节点时
    **原样透传** ``depth``，因此并列循环不会互相累加。
    """

    def __init__(self) -> None:
        self.max_loop_nesting = 0
        self.loop_count = 0
        self.function_count = 0
        self.class_count = 0

    def _walk(self, node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            self._visit_child(child, depth)

    def _visit_child(self, child: ast.AST, depth: int) -> None:
        if isinstance(child, _LOOP_NODES):
            self.loop_count += 1
            new_depth = depth + 1
            self.max_loop_nesting = max(self.max_loop_nesting, new_depth)
            self._walk(child, new_depth)
        else:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.function_count += 1
            elif isinstance(child, ast.ClassDef):
                self.class_count += 1
            self._walk(child, depth)

    def run(self, tree: ast.AST) -> None:
        self._walk(tree, 0)


def count_lines(source: str) -> dict[str, int]:
    """用 tokenize 统计代码行 / 注释行 / 文档字符串行 / 空行。

    空白与注释在 tokenize 中被显式识别，因此字符串里的 ``#`` 不会被误算。
    """
    total = len(source.splitlines())
    code_lines = comment_lines = 0
    comment_line_numbers: set[int] = set()
    code_line_numbers: set[int] = set()

    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                # 整行注释才算注释行；行尾注释不计入，避免夸大注释率
                if not source.splitlines()[tok.start[0] - 1][: tok.start[1]].strip():
                    comment_line_numbers.add(tok.start[0])
            elif tok.type not in (
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENDMARKER,
                tokenize.ENCODING,
            ):
                code_line_numbers.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):
        # 语法不完整时退化为朴素统计，不抛异常打断审查流程
        for idx, line in enumerate(source.splitlines(), start=1):
            if line.strip().startswith("#"):
                comment_line_numbers.add(idx)
            elif line.strip():
                code_line_numbers.add(idx)

    code_lines = len(code_line_numbers)
    comment_lines = len(comment_line_numbers)
    docstring_lines = _count_docstring_lines(source)
    blank_lines = max(total - code_lines - comment_lines, 0)

    denominator = max(code_lines, 1)
    return {
        "total_lines": total,
        "code_lines": code_lines,
        "comment_lines": comment_lines,
        "docstring_lines": docstring_lines,
        "blank_lines": blank_lines,
        "comment_ratio": round(comment_lines / denominator * 100, 1),
    }


def _count_docstring_lines(source: str) -> int:
    """统计模块/类/函数文档字符串占用的行数。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0

    lines = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            start = first.lineno
            end = getattr(first, "end_lineno", start)
            lines += end - start + 1
    return lines


def collect_structure(tree: ast.AST, source: str) -> StructureMetrics:
    visitor = _StructureVisitor()
    visitor.run(tree)
    line_stats = count_lines(source)
    return StructureMetrics(
        max_loop_nesting=visitor.max_loop_nesting,
        loop_count=visitor.loop_count,
        function_count=visitor.function_count,
        class_count=visitor.class_count,
        **line_stats,
    )
