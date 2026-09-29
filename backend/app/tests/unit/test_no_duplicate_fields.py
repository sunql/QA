"""类体重复字段守卫（config 重复字段批）。

Python 对「同一个类体里同名字段赋值两次」**零告警**：后者静默覆盖前者，前一份变成
死代码——而 pydantic 模型下这意味着「文件里读到的声明值 ≠ 运行时的生效值」
（本批修的 `Settings.jwtTtlSeconds` 声明 3600 / 生效 86400 即此形态）。
这类腐化只能靠 AST 守卫拦住，故本测试扫描全 `app/` 树。

与 `test_no_duplicate_methods.py` 同源同构（M6 守卫的方法版），但字段版有一个方法版
没有的合法形态要放行：**无注解的同类常量覆盖**（`X = 1` 之后 `X = 2`，普通类里
是合法的迭代式赋值，不是腐化）——守卫只报「带注解的声明」(`AnnAssign`) 被重复。

守卫本身也要被守卫：`test_guard_detects_a_synthetic_duplicate_field` 用合成源码
证明它**能**报出重复（否则一个恒返回 [] 的实现会让全树断言永远静默通过）。
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from app import __file__ as appInitFile

_APP_DIR = Path(appInitFile).resolve().parent

# 类体内可继续下钻的复合语句（不含 def/class：那是另一个作用域）
_COMPOUND_STMTS = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor)

_Target = ast.AnnAssign


def _collectTargets(body: list[ast.stmt], found: dict[str, list[_Target]]) -> None:
    for stmt in body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            found[stmt.target.id].append(stmt)
        elif isinstance(stmt, ast.Match):
            # match 的 case body 与 if/try 的 body 同为类体作用域（但不在 stmt.body 上）
            for case in stmt.cases:
                _collectTargets(case.body, found)
        elif isinstance(stmt, _COMPOUND_STMTS):
            _collectTargets(stmt.body, found)
            _collectTargets(getattr(stmt, "orelse", []), found)
            _collectTargets(getattr(stmt, "finalbody", []), found)


def _findDuplicateFields(tree: ast.AST, relPath: str) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        found: dict[str, list[_Target]] = defaultdict(list)
        _collectTargets(node.body, found)
        for name, defs in found.items():
            if len(defs) < 2:
                continue
            lines = sorted(d.lineno for d in defs)
            problems.append(f"{relPath} :: {node.name} :: {name} @ {lines}")
    return problems


def _scanSource(source: str, relPath: str = "<synthetic>") -> list[str]:
    return _findDuplicateFields(ast.parse(source), relPath)


def test_guard_detects_a_synthetic_duplicate_field() -> None:
    problems = _scanSource(
        "class A:\n"
        "    dup: int = 1\n"
        "    other: int = 2\n"
        "    dup: int = 3\n"
    )
    assert problems == ["<synthetic> :: A :: dup @ [2, 4]"], problems


def test_guard_reports_annotation_then_annotation() -> None:
    """生效语义不明的最危险形态：`Optional` 声明在前、非 Optional 覆盖在后。"""
    problems = _scanSource(
        "class A:\n"
        "    when: object | None = None\n"
        "    when: object\n"
    )
    assert problems == ["<synthetic> :: A :: when @ [2, 3]"], problems


def test_guard_tolerates_bare_assignment_reassignment() -> None:
    """无注解的同名赋值是普通类常量迭代的合法形态，不得误报（只盯带注解的声明）。"""
    problems = _scanSource(
        "class A:\n"
        "    STATE = 1\n"
        "    STATE = 2\n"
    )
    assert problems == [], problems


def test_guard_tolerates_bare_assignment_after_annotation() -> None:
    """先声明类型、后无注解覆值（`x: int = 1` 然后 `x = 2`）同样是合法迭代。"""
    problems = _scanSource(
        "class A:\n"
        "    state: int = 1\n"
        "    state = 2\n"
    )
    assert problems == [], problems


def test_guard_detects_duplicate_defined_inside_a_match_case() -> None:
    """`match` 的 case body 也是类体作用域，必须下钻（与方法守卫同款漏检面）。"""
    problems = _scanSource(
        "class A:\n"
        "    dup: int = 1\n"
        "    match 1:\n"
        "        case 1:\n"
        "            dup: int = 2\n"
    )
    assert problems == ["<synthetic> :: A :: dup @ [2, 5]"], problems


def test_guard_ignores_fields_nested_inside_a_method() -> None:
    """方法体内的局部注解赋值是另一个作用域，不参与类体统计。"""
    problems = _scanSource(
        "class A:\n"
        "    def build(self):\n"
        "        x: int = 1\n"
        "        x: int = 2\n"
        "        return x\n"
    )
    assert problems == [], problems


def test_guard_ignores_same_name_across_different_classes() -> None:
    """不同类里的同名字段互不相干。"""
    problems = _scanSource(
        "class A:\n"
        "    x: int = 1\n"
        "class B:\n"
        "    x: int = 2\n"
    )
    assert problems == [], problems


def test_no_duplicate_field_declarations_anywhere_in_app() -> None:
    problems: list[str] = []
    for path in sorted(_APP_DIR.rglob("*.py")):
        relPath = str(path.relative_to(_APP_DIR))
        problems.extend(_findDuplicateFields(ast.parse(path.read_text(encoding="utf-8")), relPath))
    assert problems == [], "发现类体带注解字段被重复声明（后者静默覆盖前者）:\n" + "\n".join(problems)
