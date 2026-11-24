"""场景分支：槽位 → 提问组装（冲突让位）—— 从前端移到后端（P4）。

设计文档：docs/superpowers/specs/2026-11-24-copilot-scenario-config-design.md

关键决策：**冲突让位与提问组装在后端**。前端只传结构化槽位 + 用户文字；
模板与让位启发式（关键词/编码模式）跟场景配置走（DB），改配置即时生效，
不再依赖前端发版。
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.exceptions import ValidationError

#: 编码模式（B109/F109 供应商码、T3/C1 基地码）——纯编码提问也算指定。
#: 注意：Python ``\b`` 在 CJK↔字母数字边界不生效（下B109 中的 B109 匹配不到，
#: 2026-11-24 生产实证），必须用显式 lookaround 声明边界。
_CODE_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])[A-Z]\d{2,4}(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z])T\d{1,2}(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z])C\d(?![0-9A-Za-z])"
)
#: 长编码（物料码：AJE0000781 / 201000000000001111D）。
_LONG_CODE_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])[A-Z]{2,}\d{4,}(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z])\d{10,}(?![0-9A-Za-z])"
)


def _mentionsKeywords(text: str, keywords: list[str] | None) -> bool:
    """关键词命中（对所有槽位类型生效）。"""
    if keywords:
        for kw in keywords:
            if kw and kw in text:
                return True
    return False


def _mentionsEntityCode(text: str) -> bool:
    """短编码/长编码命中（只对实体类槽位生效——编码是某个实体的标识，
    不代表时间窗等数值维度被指定）。"""
    return bool(_CODE_PATTERN.search(text) or _LONG_CODE_PATTERN.search(text))


def _fmtValue(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return str(value)


def _fmtArgs(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _renderTemplate(template: str, slots: dict[str, Any]) -> str:
    """``{slot}`` 占位替换（str.replace 而非 str.format：模板里可能有其他花括号）。
    语义与 conflictKeywords 同源：场景配置驱动，改配置即时生效。"""
    out = template
    for key, value in slots.items():
        out = out.replace("{" + str(key) + "}", _fmtValue(value))
    return out


def renderScenarioQuestion(
    *,
    template: str,
    slots: dict[str, Any],
    slotSchema: list[dict[str, Any]],
    message: str | None,
) -> str:
    """按场景模板渲染最终提问。

    - ``message``（用户补充文字）为主句；**文字提到的维度让位**——该槽位
      不进参数尾注（冲突以用户问题描述为准，2026-11-24 生产口径）；
    - 让位判定两层启发式：``conflictKeywords``（对所有槽位生效）+
      编码模式（仅对实体类槽位 entityMulti/dictMulti 生效）；
    - ``template``（场景配置的主句）：用户无补充文字时作为 lead，
      ``{slot}`` 占位用槽位值替换（str 原样、list 用「、」连接）；
    - 尾注附「冲突以用户问题描述为准」仲裁指令（双保险，规划器是最终仲裁）。

    :raises ValidationError: 用户文字为空且模板要求的槽位缺失。
    """
    lead = (message or "").strip()

    schemaByKey = {str(item.get("key", "")): item for item in slotSchema}
    missing: list[str] = []
    for key, item in schemaByKey.items():
        if item.get("required") and not str(slots.get(key) or "").strip():
            missing.append(str(item.get("label") or key))
    if missing and not lead:
        raise ValidationError(f"缺少场景必填槽位：{'、'.join(missing)}")

    # 冲突让位：文字命中的维度不进参数段
    tailParts: list[str] = []
    argsParts: list[str] = []
    for key, item in schemaByKey.items():
        value = slots.get(key)
        if value is None or (isinstance(value, list) and not value):
            continue
        keywords = item.get("conflictKeywords") or []
        mentioned = _mentionsKeywords(lead, list(keywords))
        if not mentioned and item.get("type") in ("entityMulti", "dictMulti"):
            mentioned = _mentionsEntityCode(lead)
        if mentioned:
            continue
        tailParts.append(f"{item.get('label') or key}={_fmtValue(value)}")
        argsParts.append(f"{key}=[{_fmtArgs(value)}]")

    leadText = lead or _renderTemplate(template, slots).strip()
    if not leadText:
        leadText = "分析待到货情况"
    partsText = "；".join(tailParts)
    argsText = f"；{'; '.join(argsParts)}" if argsParts else ""
    arbiter = "若上方括号内的参数与用户问题描述冲突，以用户问题描述为准。"
    return f"{leadText}（{partsText}）。请调用场景绑定工具完成取数与分析{argsText}。{arbiter}"
