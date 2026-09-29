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

# 合法同名重定义的「角色」：装饰器后缀 → 角色名
_REDEFINITION_ROLES = {
    "overload": "overload",
    "property": "getter",
    ".getter": "getter",
    ".setter": "setter",
    ".deleter": "deleter",
}

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


def _redefinitionRole(fn: _Def) -> str | None:
    """该 def 在同名组里扮演的重定义角色；普通方法返回 None。"""
    for d in fn.decorator_list:
        name = _decoratorName(d)
        for suffix, role in _REDEFINITION_ROLES.items():
            if name.endswith(suffix):
                return role
    return None


def _isLegitimateRedefinition(defs: list[_Def]) -> bool:
    """两种合法同名重定义（其余一律照报）：

    - **overload**：若干 `@overload` 存根 + 至多 1 个无装饰器的实现；
    - **property 家族**：`getter`/`setter`/`deleter` 每种角色至多一次。
      （两个 `@property` 同名 getter、或两个 `@x.setter` 都是真重复，必须照报。）
    """
    roles = [_redefinitionRole(d) for d in defs]
    if None in roles:
        rest = [r for r in roles if r is not None]
        return roles.count(None) == 1 and all(r == "overload" for r in rest)
    if all(r == "overload" for r in roles):
        return True
    return len(roles) == len(set(roles))


def _collectDefs(body: list[ast.stmt], found: dict[str, list[_Def]]) -> None:
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[stmt.name].append(stmt)
        elif isinstance(stmt, ast.Match):
            # match 的 case body 与 if/try 的 body 同为类体作用域（但不在 stmt.body 上）
            for case in stmt.cases:
                _collectDefs(case.body, found)
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
            if len(defs) < 2 or _isLegitimateRedefinition(defs):
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


def test_guard_tolerates_overload_with_its_concrete_implementation() -> None:
    """标准 overload 形态 = 若干 @overload 存根 + 1 个无装饰器的实现，不得误报。

    （只有存根、没有实现是**半截**写法；真实代码里实现必然在，故这条必须放行。）
    """
    problems = _scanSource(
        "class A:\n"
        "    @overload\n"
        "    def f(self, x: int) -> int: ...\n"
        "    @overload\n"
        "    def f(self, x: str) -> str: ...\n"
        "    def f(self, x): return x\n"
    )
    assert problems == [], problems


def test_guard_still_reports_two_getters_alongside_an_implementation() -> None:
    """放宽 overload 后不得连带放行「两个 getter + 一个普通实现」这种真重复。"""
    problems = _scanSource(
        "class A:\n"
        "    @property\n"
        "    def name(self): return self._n\n"
        "    @property\n"
        "    def name(self): return self._other\n"
        "    def name(self): return 3\n"
    )
    assert problems == ["<synthetic> :: A :: name @ [3, 5, 6]"], problems


def test_guard_detects_duplicate_defined_inside_a_match_case() -> None:
    """`match` 的 case body 也是类体作用域，必须下钻（不能漏检）。"""
    problems = _scanSource(
        "class A:\n"
        "    def dup(self): return 1\n"
        "    match 1:\n"
        "        case 1:\n"
        "            def dup(self): return 2\n"
    )
    assert problems == ["<synthetic> :: A :: dup @ [2, 5]"], problems


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
