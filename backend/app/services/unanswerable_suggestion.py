"""不可回答建议：缺表/缺术语推断（4-2）。

计划 target=无法回答 时，从用户问题中提取候选术语（引号/强调框定片段），
与本体现在可回答的类/属性/别名/表/列做匹配，未命中即为"缺失术语"；
无候选术语时回退为列举现有业务类，引导用户从已有数据选题。

纯函数模块，无 IO；由 chat_service 在不可回答路径调用，不改变被测逻辑。
"""

from __future__ import annotations

import re
from typing import Any

# 缺术语建议中最多列举的现有业务类数量（避免回答过长淹没用户）。
_UNANSWERABLE_AVAILABLE_CLASS_LIMIT = 6
# 候选术语提取：引号/强调框定片段（中文问题无分词，仅信任显式框定的词；
# 不做整句/拉丁词切分，避免把 SQL 关键字等误报为缺失术语）。
_QUOTED_TERM_PATTERN = re.compile(r"[「『【“\"'（(]([^」』】”\"'）)]{1,20})[」』】”\"'）)]")
# 无可推断建议时的兜底引导句（与旧固定文案语义一致）。
_UNANSWERABLE_SUGGESTION_FALLBACK = "您可以换一种问法，或尝试询问系统中已有的业务数据。"


def _ontologySurfaceTerms(classes: list[Any]) -> set[str]:
    """收集本体现在可回答的表面词（类名/别名/表名/属性/列/指标）。

    用于与问题候选术语做匹配：命中的即"本体里有"，未命中的即"缺失术语"。
    返回新集合，不改变入参。空类/空字段自动忽略。
    """
    terms: set[str] = set()
    for cls in classes:
        terms.update(term for term in (cls.class_name, cls.class_alias, cls.source_table) if term)
        for prop in cls.properties:
            terms.update(
                term for term in (prop.property_name, prop.property_alias, prop.source_column) if term
            )
            if prop.business_aliases:
                terms.update(alias for alias in prop.business_aliases if alias)
        for metric in cls.metrics:
            terms.update(term for term in (metric.metric_name, metric.metric_alias) if term)
    return terms


def _extractCandidateTerms(question: str) -> list[str]:
    """从问题提取可能指代业务术语的候选片段：引号/强调框定片段。

    中文问题无天然分词，仅信任显式框定（「…」『…』“…”"…"『…』【…】（…）），
    避免把整句或无关词当术语误报缺失。返回去重后的新列表。
    """
    candidates: list[str] = []
    for match in _QUOTED_TERM_PATTERN.findall(question):
        term = match.strip()
        if term and term not in candidates:
            candidates.append(term)
    return candidates


def _matchesTerm(term: str, vocabulary: set[str]) -> bool:
    """术语是否命中本体表面词：双向子串包含都算命中（近似/缩写可对得上）。"""
    lowered = term.lower()
    return any(lowered in entry or entry in lowered for entry in vocabulary)


def _buildUnanswerableSuggestion(question: str, classes: list[Any]) -> str:
    """4-2：不可回答时推断缺表/缺术语建议，帮助用户定向修正。

    优先报告问题中未命中的候选术语（用户提了但本体里没有）；无候选术语时
    回退为列举现有业务类，引导用户从已有数据选题。返回建议句（可为空串，
    由调用方决定是否拼兜底引导），不改变入参。
    """
    vocabulary = _ontologySurfaceTerms(classes)
    missing = [
        term for term in _extractCandidateTerms(question) if not _matchesTerm(term, vocabulary)
    ]
    if missing:
        joined = "、".join(missing[: _UNANSWERABLE_AVAILABLE_CLASS_LIMIT])
        return f"您提到的『{joined}』未匹配到现有数据表或字段，请检查名称或换个说法。"
    available: list[str] = []
    for cls in classes:
        if cls.class_name and cls.class_name not in available:
            available.append(cls.class_name)
    if available:
        shown = "、".join(available[:_UNANSWERABLE_AVAILABLE_CLASS_LIMIT])
        return f"当前可查询的业务数据包括：{shown}。"
    return ""


__all__ = [
    "_buildUnanswerableSuggestion",
    "_UNANSWERABLE_SUGGESTION_FALLBACK",
]
