"""启发式代码味道检查。

每条规则都要能说清「为什么这是问题」，否则不该出现在结果里。
不引入 LLM 判断，纯静态规则 —— 可预测、可测试、零成本。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(slots=True)
class Finding:
    code: str
    severity: str  # info | warning | error
    line: int | None
    message: str


_TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def run_heuristics(tree: ast.AST) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_check_bare_except(tree))
    findings.extend(_check_mutable_defaults(tree))
    findings.extend(_check_none_comparison(tree))
    findings.extend(_check_unreachable(tree))
    findings.extend(_check_broad_except(tree))
    findings.sort(key=lambda f: (f.line or 0, f.code))
    return findings


def _check_bare_except(tree: ast.AST) -> list[Finding]:
    out: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            out.append(
                Finding(
                    code="BARE_EXCEPT",
                    severity="warning",
                    line=node.lineno,
                    message="裸 except 会吞掉 KeyboardInterrupt / SystemExit，请显式捕获异常类型",
                )
            )
    return out


def _check_mutable_defaults(tree: ast.AST) -> list[Finding]:
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                out.append(
                    Finding(
                        code="MUTABLE_DEFAULT",
                        severity="warning",
                        line=node.lineno,
                        message=f"函数 {node.name} 使用可变对象作默认参数，会在多次调用间共享状态",
                    )
                )
    return out


def _check_none_comparison(tree: ast.AST) -> list[Finding]:
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant):
                if comparator.value is None:
                    out.append(
                        Finding(
                            code="NONE_COMPARISON",
                            severity="info",
                            line=node.lineno,
                            message="与 None 比较应使用 is / is not，避免自定义 __eq__ 干扰",
                        )
                    )
    return out


def _check_unreachable(tree: ast.AST) -> list[Finding]:
    """同一语句块内，终结语句之后仍有语句 → 不可达。

    只在同一 ``body`` 内判断，不跨分支，避免误报。
    """
    out: list[Finding] = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for idx, stmt in enumerate(block[:-1]):
                if isinstance(stmt, _TERMINATORS):
                    nxt = block[idx + 1]
                    out.append(
                        Finding(
                            code="UNREACHABLE_CODE",
                            severity="warning",
                            line=getattr(nxt, "lineno", None),
                            message=f"第 {getattr(stmt, 'lineno', '?')} 行的 {type(stmt).__name__.lower()} "
                            "之后的语句永远不会执行",
                        )
                    )
                    break
    return out


def _check_broad_except(tree: ast.AST) -> list[Finding]:
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        names = (
            [node.type.id]
            if isinstance(node.type, ast.Name)
            else [e.id for e in node.type.elts if isinstance(e, ast.Name)]
            if isinstance(node.type, ast.Tuple)
            else []
        )
        if "Exception" in names or "BaseException" in names:
            out.append(
                Finding(
                    code="BROAD_EXCEPT",
                    severity="info",
                    line=node.lineno,
                    message=f"捕获了过宽的异常类型 {names}，建议按具体异常分类处理",
                )
            )
    return out
