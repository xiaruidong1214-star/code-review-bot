"""命令行入口：不依赖 Redis 也能对单文件跑一遍静态分析。

设计意图：审查工具的核心价值应该能在没有基础设施的情况下被人用起来，
这也是测试与 CI 里的主要使用方式。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from reviewbot.analysis import analyze_python
from reviewbot.analysis.analyzer import normalize_source


def _analyze_file(path: Path, as_json: bool) -> int:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"无法读取 {path}: {exc}", file=sys.stderr)
        return 2

    result = analyze_python(source)

    if as_json:
        payload = {
            "language": result.language,
            "structure": asdict(result.structure),
            "recursion": asdict(result.recursion),
            "findings": [asdict(f) for f in result.findings],
            "parse_error": result.parse_error,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if result.parse_error else 0

    if result.parse_error:
        print(f"解析失败：{result.parse_error}")
        return 1

    s = result.structure
    print(f"文件：{path}")
    print(f"  总行数 {s.total_lines}（代码 {s.code_lines} / 注释 {s.comment_lines} / 空行 {s.blank_lines}）")
    print(f"  注释率 {s.comment_ratio}%")
    print(f"  最大循环嵌套 {s.max_loop_nesting}，循环 {s.loop_count} 个，函数 {s.function_count} 个")
    if result.recursion.direct_recursive:
        print(f"  直接递归：{', '.join(result.recursion.direct_recursive)}")
    for group in result.recursion.mutually_recursive_groups:
        print(f"  互递归环：{' → '.join(group)}")
    if result.findings:
        print("  发现：")
        for finding in result.findings:
            print(f"    [{finding.severity}] L{finding.line} {finding.code}: {finding.message}")
    else:
        print("  发现：无")
    print(f"  注：{s.approximation_notice}")
    return 0


def _dump_fingerprint(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    fingerprint = normalize_source(source)
    if fingerprint is None:
        print("无法解析，无指纹", file=sys.stderr)
        return 1
    print(fingerprint)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reviewbot", description="代码审查工具（静态分析部分，无需 Redis）")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="分析一个 Python 文件")
    analyze.add_argument("path", type=Path)
    analyze.add_argument("--json", action="store_true", help="以 JSON 输出")

    fingerprint = sub.add_parser("fingerprint", help="打印 AST 归一化指纹（用于核对缓存 key）")
    fingerprint.add_argument("path", type=Path)

    args = parser.parse_args(argv)
    if args.command == "analyze":
        return _analyze_file(args.path, args.json)
    if args.command == "fingerprint":
        return _dump_fingerprint(args.path)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
