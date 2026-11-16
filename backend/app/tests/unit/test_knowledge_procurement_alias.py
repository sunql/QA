"""P4 采购薄别名 —— 「无逻辑」守卫（D36 判据的可检查形态）。

G5 第二条判据是「采购薄别名**无逻辑**」。这条判据如果不落成**机器可判**的东西，
就只是一句口号 ⇒ 本文件把它变成三条守卫：

1. **AST 守卫**：别名模块的源码里**不许出现任何分支 / 循环**
   （`if` / `for` / `while` / `match`）—— 一旦有人在别名里加「采购专属分支」，
   立刻转红。
2. **导入守卫**：别名只允许导入「契约模块 + 编排器 + 框架件」；
   不许导入任何**业务服务**（领域服务 / 数据服务 / LLM 客户端 …）。
3. **路由守卫**：别名只暴露**一个**端点 `/query`。

⚠️ 本文件在 `unit/` 下 ⇒ 允许新建。
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.domain.knowledge_schemas import (
    PROCUREMENT_DOMAIN_CODE,
    ProcurementKnowledgeQueryRequest,
)

#: 别名源码路径（**相对本文件**定位 ⇒ 不依赖 pytest 的 CWD）。
_ALIAS_MODULE_PATH = Path(__file__).resolve().parents[3] / "app/api/v1/procurement_knowledge.py"

#: 别名允许导入的模块前缀（框架件 + 契约 + 编排器）。
_ALLOWED_IMPORT_PREFIXES: tuple[str, ...] = (
    "__future__",
    "fastapi",
    "sqlalchemy",
    "app.api.v1.knowledge_facade",
    "app.dependencies",
    "app.domain.knowledge_schemas",
    "app.infrastructure.rate_limit",
)

#: 出现即视为「有逻辑」的 AST 节点。
_BRANCH_NODES: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Match,
    ast.Try,
    ast.IfExp,
)


def _aliasSource() -> str:
    return _ALIAS_MODULE_PATH.read_text(encoding="utf-8")


def _branchNodes(source: str) -> list[str]:
    """返回源码里的「有逻辑」节点（可被单独测试 ⇒ 守卫本身不会假绿）。"""
    tree = ast.parse(source)
    return [
        f"{type(node).__name__}@{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, _BRANCH_NODES)
    ]


def test_aliasModuleHasNoBranchOrLoopNodes() -> None:
    """守卫 1：别名里不许有分支 / 循环 / try —— 它只该「转发」。"""
    offenders = _branchNodes(_aliasSource())

    assert offenders == [], (
        "采购薄别名必须无业务逻辑，但源码里出现了分支 / 循环 / try："
        + ", ".join(offenders)
    )


def test_branchGuardItselfDetectsBranches() -> None:
    """守卫的自检：合成一段**有分支**的源码，守卫必须能抓到。

    否则守卫可能因为「节点类型改名 / 解析路径变了」而**恒绿**（假绿守卫）。
    """
    assert _branchNodes("def f(x):\n    if x:\n        return 1\n    return 0\n") != []
    assert _branchNodes("def f(x):\n    return x\n") == []


def test_aliasImportsNoBusinessService() -> None:
    """守卫 2：别名不许导入业务服务（逻辑只能有一份，在编排器里）。"""
    tree = ast.parse(_aliasSource())

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.append(module)
            imported.extend(f"{module}.{alias.name}" for alias in node.names)

    offenders = sorted(
        {
            name
            for name in imported
            if name and not name.startswith(_ALLOWED_IMPORT_PREFIXES)
        }
    )
    assert offenders == [], (
        f"采购薄别名只允许导入框架件 / 契约 / 编排器，实际多出：{offenders}"
    )


def test_aliasExposesExactlyOneQueryEndpoint() -> None:
    """守卫 3：别名只暴露一个端点 `/query`（没有第二个「采购专属」入口）。"""
    from app.api.v1 import procurement_knowledge

    routes = [
        (sorted(route.methods)[0], route.path)
        for route in procurement_knowledge.router.routes
    ]

    assert routes == [("POST", "/query")]


def test_aliasRequestHasNoDomainFieldAndFixesProcurement() -> None:
    """请求体里**根本没有 domain** ⇒ 调用方没有机会把请求引到别的域。"""
    request = ProcurementKnowledgeQueryRequest(question="交付延迟最高的供应商？")

    assert "domain" not in ProcurementKnowledgeQueryRequest.model_fields
    assert request.toCanonicalRequest().domain == PROCUREMENT_DOMAIN_CODE
