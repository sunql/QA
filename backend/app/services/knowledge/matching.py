"""P4 知识检索的**确定性词项匹配**（本模块**不调 LLM**）。

为什么不用 LLM
--------------
G5 的三条验收（试点问题集端到端 / 权限双向 / 引用可回溯）必须**客观可判**。
引入 LLM 会把「答案好不好」这一**主观**维度混进来，并且带来计量 / 预算 / 评测
三张皮（chat 通道已经有一份 LLM 编排，Facade 再来一份必然口径漂移）。

⇒ 本模块用**词项命中**做召回：可复现、可解释、可测。

匹配口径
--------
1. 词项来自发布快照的**名字与别名**（`className` / `classAlias` /
   `propertyName` / `propertyAlias` / `businessAliases` / `metricName` /
   `kpiCode` / `kpiName` / `semanticKeywords` …），**不碰散文描述**
   （`description` / `businessDefinition`）—— 散文命中噪声太大，且同一段散文
   会被几十个条目共享 ⇒ 排序失去区分度。
2. **CJK 词项**走子串包含（中文没有词边界）；**ASCII 词项**走词边界匹配
   （否则 `po` 会命中 `portfolio`）。ASCII 词项额外允许**复数形式**
   （`supplier` 命中 `suppliers`）。
3. **复合标识符**（`PurchaseOrder` / `purchase_order`）额外支持**全词命中**：
   切出的每个词都出现在问题里即算命中，权重降一档。
4. 命中**按词项文本去重**计分：同一个别名写两遍不加分（否则别名多的条目虚高）。
5. **单字符词项不参与匹配**（`料` 能命中任何含「料」的问题）。
6. **自然问法（I2-A，2026-11-21）**：严格包含（词项 ⊂ 问句）之外，再补两个方向——
   ① **去类名前缀**：`DWD_/DIM_/ODS_`（含别名写法 `DWD收货单宽表`）剥掉后重试严格包含；
   ② **片段命中**：词项里的 ≥3 字 CJK 片段 / ≥3 字符 ASCII 词出现在问句里即算命中（降两档）。
   实测动因：别名「供应商主数据」对自然问法「供应商」永不命中 ⇒ 四通道 0 证据。
   噪声守卫：片段最小 3 字（避开「数据/公司」这类 2 字常用词）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: 参与匹配的词项最短长度（单字噪声太大）。
MIN_TERM_LENGTH = 2

#: 词项权重档位：别名 > 名字 > 复合标识符的全词命中（降一档）。
WEIGHT_NAME = 3
WEIGHT_ALIAS = 4
WEIGHT_TOKEN_MATCH = 2

#: 片段文本默认上限（响应体不该被单个长描述撑爆）。
DEFAULT_CLIP_LIMIT = 240

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_IDENT_SPLIT_RE = re.compile(r"[^0-9A-Za-z一-鿿]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CJK_RE = re.compile(r"[一-鿿]")


@dataclass(frozen=True, slots=True)
class TermSpec:
    """一个参与匹配的词项（文本 + 权重 + 是否允许放宽匹配）。

    ``allowPartial``：是否参与 I2-A 的两档放宽（去前缀 / 片段命中）。
    **只给类名/类别名与指标名/别名置 True**——属性词项保持精确（否则
    SQL schema 会被无关属性撑大：实测「供应商 交付天数」把 `supplier_name` 也带进来）。
    """

    text: str
    weight: int = WEIGHT_NAME
    allowPartial: bool = False


@dataclass(frozen=True, slots=True)
class MatchedTerm:
    """命中的词项（保留原文，便于在响应里解释「为什么命中」）。"""

    text: str
    weight: int


def normalizeText(text: str) -> str:
    """归一：去首尾空白 + 小写。"""
    return text.strip().lower()


def hasCjk(text: str) -> bool:
    """是否含中文字符。"""
    return bool(_CJK_RE.search(text))


def splitIdentifier(text: str) -> tuple[str, ...]:
    """把复合标识符切成词：`PurchaseOrder` / `purchase_order` → `("purchase","order")`。

    中文标识符原样返回（中文没有大小写/分隔符可切）。
    """
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", text)
    parts = _IDENT_SPLIT_RE.split(spaced)
    return tuple(part.lower() for part in parts if part)


def _asciiTermMatches(term: str, question: str) -> bool:
    """ASCII 词项的词边界匹配（含复数形式）。"""
    if re.search(rf"(?<![0-9a-z]){re.escape(term)}(?![0-9a-z])", question):
        return True
    if len(term) >= 3:
        return bool(
            re.search(rf"(?<![0-9a-z]){re.escape(term)}s(?![0-9a-z])", question)
        )
    return False


def _containsTerm(term: str, question: str) -> bool:
    """词项是否出现在问题里（按语言分两种口径）。"""
    if hasCjk(term):
        return term in question
    return _asciiTermMatches(term, question)


#: 类名/别名常见前缀（`DWD_` / `DIM_` / `ODS_`，也覆盖 `DWD收货单宽表` 这种无下划线写法）。
_CLASS_PREFIX_RE = re.compile(r"^(dwd|dim|ods)[_\-\s]?", re.IGNORECASE)

#: 片段命中的最短片段（2 字/词噪声太大：「数据」「公司」）。
PARTIAL_MIN_FRAGMENT = 3


def stripClassPrefix(text: str) -> str:
    """剥掉类名前缀（`DWD_GOODS_RECEIPT_DTL` / `DWD收货单宽表` → 去前缀）。"""
    return _CLASS_PREFIX_RE.sub("", text)


def _partialFragmentSize(term: str, question: str) -> int:
    """词项里出现在问句中的**最长**片段长度（0 = 无片段命中）。

    口径：CJK 词项按 3–6 字滑窗；ASCII 词项按 ≥3 字符整词（`supplier` ⊂ `dim_supplier`）。
    片段方向是「**词项片段 ⊂ 问句**」——与严格包含相反，用于自然问法（问句只说
    别名的一部分）。
    """
    if not term or not question:
        return 0
    best = 0
    if hasCjk(term):
        maxSize = min(6, len(term))
        for size in range(maxSize, PARTIAL_MIN_FRAGMENT - 1, -1):
            for start in range(0, len(term) - size + 1):
                if term[start : start + size] in question:
                    return size
        return 0
    # ASCII：以「词」为片段（避免子串误命中，如 po ⊂ portfolio）
    for part in splitIdentifier(term):
        if len(part) >= PARTIAL_MIN_FRAGMENT and part in question:
            best = max(best, len(part))
    return best


def matchTerms(question: str, terms: Sequence[TermSpec]) -> tuple[MatchedTerm, ...]:
    """命中的词项（去重、按出现顺序）。

    Args:
        question: 用户问题（原文）。
        terms: 候选词项。

    Returns:
        命中的词项列表；没命中返回空元组（**不是** None，便于直接判空）。
    """
    questionNorm = normalizeText(question)
    if not questionNorm:
        return ()
    questionTokens = frozenset(_ASCII_TOKEN_RE.findall(questionNorm))

    matched: list[MatchedTerm] = []
    seen: set[str] = set()
    for spec in terms:
        text = (spec.text or "").strip()
        if not text:
            continue
        norm = normalizeText(text)
        if len(norm) < MIN_TERM_LENGTH or norm in seen:
            continue
        if _containsTerm(norm, questionNorm):
            seen.add(norm)
            matched.append(MatchedTerm(norm, spec.weight))
            continue
        # ① 去类名前缀后重试严格包含（降一档；仅放宽词项）
        stripped = stripClassPrefix(norm) if spec.allowPartial else norm
        if stripped and stripped != norm and _containsTerm(stripped, questionNorm):
            seen.add(norm)
            matched.append(MatchedTerm(norm, max(1, spec.weight - 1)))
            continue
        # ② 片段命中：词项的 ≥3 字片段出现在问句里（降两档；仅放宽词项）
        for candidate in (
            ((norm, stripped) if stripped else (norm,)) if spec.allowPartial else ()
        ):
            size = _partialFragmentSize(candidate, questionNorm)
            if size >= PARTIAL_MIN_FRAGMENT:
                seen.add(norm)
                matched.append(MatchedTerm(norm, max(1, spec.weight - 2)))
                break
        else:
            parts = splitIdentifier(text)
            if len(parts) > 1 and all(part in questionTokens for part in parts):
                seen.add(norm)
                matched.append(MatchedTerm(norm, max(1, spec.weight - 1)))
    return tuple(matched)


def sortScored(
    items: Sequence[tuple[int, str, Any]],
) -> tuple[tuple[int, str, Any], ...]:
    """按 (-得分, key) 排序 —— 同分用 key 兜底 ⇒ 输出顺序**确定可复现**。

    Args:
        items: `(得分, 唯一键, 载荷)` 三元组。
    """
    return tuple(sorted(items, key=lambda item: (-item[0], item[1])))


# ---------------------------------------------------------------------------
# 词项抽取（各路由共用；口径不许各写一份）
# ---------------------------------------------------------------------------


def textOrNone(value: Any) -> str | None:
    """非空字符串才返回值（None / 空串 / 非字符串一律当「没有」）。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def clipText(text: str, limit: int = DEFAULT_CLIP_LIMIT) -> str:
    """截断到 `limit` 字符（超长时末尾加省略号）。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def propertyTerms(prop: Mapping[str, Any]) -> tuple[TermSpec, ...]:
    """属性的可匹配词项：属性名 / 属性别名 / 业务别名。"""
    terms: list[TermSpec] = []
    name = textOrNone(prop.get("propertyName"))
    if name:
        terms.append(TermSpec(name, WEIGHT_NAME))
    alias = textOrNone(prop.get("propertyAlias"))
    if alias:
        terms.append(TermSpec(alias, WEIGHT_ALIAS))
    businessAliases = prop.get("businessAliases")
    if isinstance(businessAliases, list):
        for item in businessAliases:
            aliasText = textOrNone(item)
            if aliasText:
                terms.append(TermSpec(aliasText, WEIGHT_ALIAS))
    return tuple(terms)


def classTerms(cls: Mapping[str, Any]) -> tuple[TermSpec, ...]:
    """类的可匹配词项：类名 / 类别名 + 各属性的词项。"""
    terms: list[TermSpec] = []
    name = textOrNone(cls.get("className"))
    if name:
        terms.append(TermSpec(name, WEIGHT_NAME, allowPartial=True))
    alias = textOrNone(cls.get("classAlias"))
    if alias:
        terms.append(TermSpec(alias, WEIGHT_ALIAS, allowPartial=True))
    properties = cls.get("properties")
    if isinstance(properties, list):
        for prop in properties:
            if isinstance(prop, Mapping):
                terms.extend(propertyTerms(prop))
    return tuple(terms)
