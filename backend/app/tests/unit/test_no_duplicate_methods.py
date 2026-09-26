"""类体重复方法守卫（M6）。

Python 对「同一个类里同名方法定义两次」**零告警**：后者静默覆盖前者，前一份变成
死代码（本批修的就是 `ChartService._buildOptionPrompt` 的两份）。这类腐化只能靠
AST 守卫拦住，故本测试扫描全 `app/` 树，而非只盯当前改动文件。

守卫本身也要被守卫：`test_guard_detects_a_synthetic_duplicate_method` 用合成源码
证明它**能**报出重复（否则一个恒返回 [] 的实现会让全树断言永远静默通过）。
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from app import __file__ as appInitFile

_APP_DIR = Path(appInitFile).resolve().parent

# 合法重复的装饰器后缀：@overload、@property/@x.setter（getter 与 setter 同名）
_ALLOWED_DECORATOR_SUFFIXES = ("overload", "property", ".setter", ".deleter", ".getter")

# 类体内可继续下钻的复合语句（不含 def/class：那是另一个作用域）
_COMPOUND_STMTS = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor)

_Def = ast.FunctionDef | ast.AsyncFunctionDef


def _decoratorName(node: ast.expr) -> str:
    """装饰器的点号名字（`ast.Attribute` 拼成 `x.setter`；`ast.Name` 直接取名）。"""
    if isinstance(node, ast.Attribute):
        return f"{_decoratorName(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _isAllowedDuplicate(fn: _Def) -> bool:
    return any(
        _decoratorName(d).endswith(_ALLOWED_DECORATOR_SUFFIXES) for d in fn.decorator_list
    )


def _collectDefs(body: list[ast.stmt], found: dict[str, list[_Def]]) -> None:
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[stmt.name].append(stmt)
        elif isinstance(stmt, _COMPOUND_STMTS):
            _collectDefs(stmt.body, found)
            _collectDefs(getattr(stmt, "orelse", []), found)
            _collectDefs(getattr(stmt, "finalbody", []), found)


def _findDuplicateMethods(tree: ast.AST, relPath: str) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        found: dict[str, list[_Def]] = defaultdict(list)
        _collectDefs(node.body, found)
        for name, defs in found.items():
            if len(defs) < 2 or all(_isAllowedDuplicate(d) for d in defs):
                continue
            lines = sorted(d.lineno for d in defs)
            problems.append(f"{relPath} :: {node.name} :: {name} @ {lines}")
    return problems


def _scanSource(source: str, relPath: str = "<synthetic>") -> list[str]:
    return _findDuplicateMethods(ast.parse(source), relPath)


def test_guard_detects_a_synthetic_duplicate_method() -> None:
    problems = _scanSource(
        "class A:\n"
        "    def dup(self):\n"
        "        return 1\n"
        "    def other(self):\n"
        "        return 2\n"
        "    def dup(self):\n"
        "        return 3\n"
    )
    assert problems == ["<synthetic> :: A :: dup @ [2, 6]"], problems


def test_guard_tolerates_overload_and_property_setter() -> None:
    problems = _scanSource(
        "class A:\n"
        "    @overload\n"
        "    def f(self, x: int) -> int: ...\n"
        "    @overload\n"
        "    def f(self, x: str) -> str: ...\n"
        "    @property\n"
        "    def name(self): return self._n\n"
        "    @name.setter\n"
        "    def name(self, v): self._n = v\n"
    )
    assert problems == [], problems


def test_guard_ignores_defs_nested_inside_a_method() -> None:
    """方法体内的局部函数是另一个作用域，不参与类体统计。"""
    problems = _scanSource(
        "class A:\n"
        "    def outer(self):\n"
        "        def inner(): return 1\n"
        "        return inner\n"
    )
    assert problems == [], problems


def test_no_duplicate_method_definitions_anywhere_in_app() -> None:
    problems: list[str] = []
    for path in sorted(_APP_DIR.rglob("*.py")):
        relPath = str(path.relative_to(_APP_DIR))
        problems.extend(_findDuplicateMethods(ast.parse(path.read_text(encoding="utf-8")), relPath))
    assert problems == [], "发现类体重复方法（后者静默覆盖前者）:\n" + "\n".join(problems)
