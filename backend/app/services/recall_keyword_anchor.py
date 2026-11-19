"""问句关键词/别名锚定（2026-11-19「问法敏感漏选」根治）。

设计：Harness/changes/2026-11-19-recall-datasource-filter-anchor/design.md。
向量召回对措辞敏感：「公司目前有多少供应商」的「目前」即可把 DIM_SUPPLIER
（别名「供应商主数据」）挤出 topK。字面包含关系对措辞不敏感 ⇒ 问句切 2–6 字
连续片段（n-gram，无分词器）与类名/别名做包含匹配，命中类强制纳入候选集、
排在向量命中之前（不被 MAX_CLASSES 截断挤掉）。上限 ANCHOR_MAX_CLASSES 由
调用方从 system_config 读后经 maxAnchored 传入。纯增量：不淘汰任何类。
"""

from __future__ import annotations

from typing import Any

_QUESTION_NGRAM_MIN = 2
_QUESTION_NGRAM_MAX = 6


def _questionFragments(question: str) -> list[str]:
    """问句 2–6 字连续片段（去空白；全量枚举，不引入分词器）。

    片段集合与枚举顺序无关：同一类的匹配取**最长命中片段**计分（精确等值优先），
    跨类排序再用类 id 升序兜底 ⇒ 确定性由打分与 id 保证，不由枚举顺序保证。
    """
    compact = "".join(question.split())
    fragments: list[str] = []
    seen: set[str] = set()
    for size in range(_QUESTION_NGRAM_MAX, _QUESTION_NGRAM_MIN - 1, -1):
        for start in range(0, max(0, len(compact) - size + 1)):
            frag = compact[start:start + size]
            if frag not in seen:
                seen.add(frag)
                fragments.append(frag)
    return fragments


def selectAnchoredClasses(
    question: str, classes: list[Any], *, maxAnchored: int
) -> list[Any]:
    """问句片段与类名/别名包含匹配，返回锚定类（新列表，不改动入参）。

    排序：精确等值（片段 == 类名/别名）优先 > 匹配片段更长优先 > 类 id 升序
    （确定性兜底）。同一类多片段命中取最高档计分。
    """
    if maxAnchored <= 0 or not question.strip() or not classes:
        return []
    fragments = _questionFragments(question)
    scored: list[tuple[int, int, int, Any]] = []
    for cls in classes:
        if cls.id is None:
            continue
        best: tuple[int, int] | None = None
        for target in (cls.class_alias, cls.class_name):
            if not target:
                continue
            normalized = "".join(target.split()).upper()
            for frag in fragments:
                fu = frag.upper()
                if fu == normalized:
                    candidate = (1, len(fu))
                elif fu in normalized:
                    candidate = (0, len(fu))
                else:
                    continue
                if best is None or candidate > best:
                    best = candidate
        if best is not None:
            scored.append((best[0], best[1], cls.id, cls))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in scored[:maxAnchored]]
