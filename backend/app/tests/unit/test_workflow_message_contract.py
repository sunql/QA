"""P5 消息模板与调用点的**契约守卫**（静态 AST）。

为什么需要这个文件
------------------

``app/services/messages_zh.py`` 的模板用 ``{snake_case}`` 命名占位符，而调用点
习惯用**驼峰**关键字实参（``domainCode=``）⇒ 两边一旦对不上，
``str.format()`` 会在**运行时**抛 ``KeyError``：

    MSG_WORKFLOW_DEFINITION_NOT_FOUND.format(domainCode=...)  # KeyError: 'domain_code'

⇒ 请求面变成 **500**（而不是可读的 404/409），而且**只在走到那条路径时**才炸。

这个坑是**静默**的：模板与调用点分处两个文件，改模板不改调用点 ⇒ 没有任何
静态检查会报出来 ⇒ 必须有一条机械守卫把它钉住。

守卫口径
--------

硬判据（必须满足，否则判红）：

1. **模板占位符必须全部被实参覆盖** —— 否则运行时 ``KeyError``。
2. **调用点必须用关键字实参** —— 位置实参无法与占位符名做静态比对，
   等于放弃这道守卫。

刻意**不**判「实参多于占位符」：``str.format()`` 会忽略多余实参（不会崩），
且部分调用点会附带上下文实参（``definitionId=`` 等）供未来模板细化 ——
把这条也判红会逼出与本守卫目的无关的改动。

范围：P5 工作流模块（``workflow_*`` + 工作流 API）。
"""

from __future__ import annotations

import ast
import string
from pathlib import Path

from app.services import messages_zh

# P5 模块清单（模板调用点所在处）。
_APP_ROOT = Path(__file__).resolve().parents[2]
_P5_MODULE_PATHS = (
    "api/v1/workflow.py",
    "services/workflow_service.py",
    "services/workflow_approval_service.py",
    "services/workflow_audit_service.py",
    "services/workflow_command_executor.py",
    "services/workflow_state_machine.py",
    "services/workflow_definition.py",
)


def _templateOf(name: str) -> str:
    """取消息模板字面量（名字不存在 ⇒ 守卫自身报红，不静默跳过）。"""

    template = getattr(messages_zh, name, None)
    assert isinstance(template, str), f"消息模板不存在：{name}"
    return template


def _placeholders(template: str) -> set[str]:
    """模板里的具名占位符集合。"""

    return {
        fieldName
        for _literal, fieldName, _spec, _conv in string.Formatter().parse(template)
        if fieldName
    }


def _formatCallSites(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """取所有 ``MSG_*.format(...)`` 调用点。"""

    sites: list[tuple[ast.Call, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "format":
            continue
        base = func.value
        if isinstance(base, ast.Name) and base.id.startswith("MSG_"):
            sites.append((node, base.id))
    return sites


def _relativePath(modulePath: str) -> str:
    return modulePath


def test_P5模块的消息调用点全部满足契约():
    """每个 ``MSG_*.format(...)`` 都必须把模板的占位符**全部**喂齐。"""

    problems: list[str] = []
    checked = 0

    for relative in _P5_MODULE_PATHS:
        path = _APP_ROOT / relative
        assert path.exists(), f"P5 模块不存在：{path}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for call, templateName in _formatCallSites(tree):
            checked += 1
            where = f"{_relativePath(relative)}:{call.lineno}"

            assert not call.args, (
                f"{where}：{templateName}.format(...) 用了位置实参 —— "
                "位置实参无法与模板占位符名做静态比对（等于放弃契约守卫）"
            )

            supplied = {kw.arg for kw in call.keywords if kw.arg}
            assert supplied, (
                f"{where}：{templateName}.format(...) 没有关键字实参 —— "
                "守卫无法比对"
            )

            needed = _placeholders(_templateOf(templateName))
            missing = sorted(needed - supplied)
            if missing:
                problems.append(
                    f"{where}：{templateName} 需要占位符 {missing}，"
                    f"但调用点只给了 {sorted(supplied)} —— "
                    "运行时 str.format() 会抛 KeyError（请求面 500）"
                )

    assert checked > 0, "没扫描到任何消息调用点（守卫空转 ⇒ 形同虚设）"
    assert not problems, "消息模板与调用点对不上：\n" + "\n".join(problems)


def test_关键模板的占位符命名与调用点一致():
    """单独钉住两个**曾经**踩过的模板（回归可见）。"""

    notFound = _placeholders(messages_zh.MSG_WORKFLOW_DEFINITION_NOT_FOUND)
    assert notFound == {"domain_code", "capability_code", "version"}, (
        f"MSG_WORKFLOW_DEFINITION_NOT_FOUND 的占位符变了：{sorted(notFound)}"
    )

    notPublished = _placeholders(messages_zh.MSG_WORKFLOW_DEFINITION_NOT_PUBLISHED)
    assert notPublished == {"version"}, (
        f"MSG_WORKFLOW_DEFINITION_NOT_PUBLISHED 的占位符变了：{sorted(notPublished)}"
    )
