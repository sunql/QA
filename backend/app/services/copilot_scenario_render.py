"""场景分支：槽位 → 提问组装（冲突让位）—— 从前端移到后端（P4）。

设计文档：docs/superpowers/specs/2026-11-24-copilot-scenario-config-design.md

关键决策：**冲突让位与提问组装在后端**。前端只传结构化槽位 + 用户文字；
让位启发式（``conflictKeywords`` / ``codePattern``）、显示格式
（``displayTemplate``）与模板全部跟场景配置走（DB），改配置即时生效，
不再依赖前端发版。
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.exceptions import ValidationError

#: 槽位 schema 中允许的启发式字段（渲染器只认这几个键，其余忽略）。
_CODE_PATTERN_KEY = "codePattern"
_DISPLAY_KEY = "displayTemplate"


def compileCodePattern(pattern: str) -> re.Pattern[str]:
    """编译槽位配置里的编码模式（保存时校验用；非法 regex 在配置时 fail loud）。

    注意：Python ``\\b`` 在 CJK↔字母数字边界不生效（「下B109」中的 B109
    匹配不到，2026-11-24 生产实证）——配置里的模式必须用显式 lookaround
    声明边界，例如 ``(?<![0-9A-Za-z])[A-Z]\\d{2,4}(?![0-9A-Za-z])``。
    """
    return re.compile(pattern)


def _mentionsKeywords(text: str, keywords: list[str] | None) -> bool:
    """关键词命中（子串包含，对所有槽位类型生效）。"""
    if keywords:
        for kw in keywords:
            if kw and kw in text:
                return True
    return False


def _mentionsSlot(lead: str, item: dict[str, Any]) -> bool:
    """冲突让位判定：文字命中的维度让位（该槽位不进尾注/参数）。

    两层启发式（均从槽位配置读取）：
    - ``conflictKeywords``：关键词子串（如「供应商」「厂家」）；
    - ``codePattern``：编码正则（如 B109 供应商码）。编码是某个实体的
      标识 ⇒ 只配置在实体类槽位上，不代表时间窗等数值维度被指定。
    """
    if not lead:
        return False
    if _mentionsKeywords(lead, item.get("conflictKeywords")):
        return True
    pattern = item.get(_CODE_PATTERN_KEY)
    if not pattern:
        return False
    try:
        return bool(re.search(str(pattern), lead))
    except re.error:
        return False  # 配置错误 fail-closed（保存时已 fail loud 拦截）


def _fmtValue(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return str(value)


def _fmtArgs(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _fmtDisplay(item: dict[str, Any], value: Any) -> str:
    """尾注显示值：``displayTemplate`` 可带 ``{value}`` 占位
    （如 时间窗 显示为「近12个月」）；未配置则渲染原始值。"""
    display = item.get(_DISPLAY_KEY)
    if display:
        return str(display).replace("{value}", _fmtValue(value))
    return _fmtValue(value)


def _renderTemplate(template: str, slots: dict[str, Any]) -> str:
    """``{slot}`` 占位替换（str.replace 而非 str.format：模板里可能有其他花括号）。"""
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
    - 让位判定从槽位配置读 ``conflictKeywords`` + ``codePattern``；
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
        if _mentionsSlot(lead, item):
            continue
        tailParts.append(
            f"{item.get('label') or key}={_fmtDisplay(item, value)}"
        )
        if isinstance(value, list):
            argsParts.append(f"{key}=[{_fmtArgs(value)}]")
        else:
            argsParts.append(f"{key}={value}")

    leadText = lead or _renderTemplate(template, slots).strip()
    if not leadText:
        leadText = "分析待到货情况"
    partsText = "；".join(tailParts)
    argsText = f"；{'; '.join(argsParts)}" if argsParts else ""
    arbiter = "若上方括号内的参数与用户问题描述冲突，以用户问题描述为准。"
    return f"{leadText}（{partsText}）。请调用场景绑定工具完成取数与分析{argsText}。{arbiter}"
