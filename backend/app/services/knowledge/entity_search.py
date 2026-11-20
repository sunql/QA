"""M2-2 实体解析（设计稿 §8.3 `GET /procurement/knowledge/entities/search`）。

复用 `EntityMappingService.searchMappings`（AutoComplete 同款检索语义：
全数字 ⇒ enterprise_key 精确 + ILIKE 兜底；GIN trigram 索引支撑 35w 行），
在其上做**分组 + 打分**：一个统一实体（enterprise_key）可能映射多个源系统
⇒ 设计稿契约是「一实体一行 + sourceMappings[] + matchScore」。

打分口径（确定性，无 LLM）：
- enterprise_key 精确命中 = **1.0**（ strongest）；
- enterprise_code 全等（大小写不敏感）= 0.9；
- enterprise_code 前缀命中 = 0.8；
- 其余（source_code / name 中缀命中）= 0.6。
排序：matchScore 降序 → enterprise_key 升序（确定性兜底）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.domain.knowledge_schemas import (
    ProcurementEntityMatchRead,
    ProcurementEntitySourceMappingRead,
)

_SCORE_EXACT_KEY = 1.0
_SCORE_CODE_EXACT = 0.9
_SCORE_CODE_PREFIX = 0.8
_SCORE_INFIX = 0.6


def _rowScore(row: Any, q: str) -> float:
    """单行命中强度（q 已 strip；调用方保证非空）。"""
    if q.isdigit() and row.enterprise_key == int(q):
        return _SCORE_EXACT_KEY
    code = (row.enterprise_code or "").upper()
    target = q.upper()
    if code == target:
        return _SCORE_CODE_EXACT
    if code.startswith(target):
        return _SCORE_CODE_PREFIX
    return _SCORE_INFIX


def buildEntityMatches(rows: list[Any], *, q: str) -> list[ProcurementEntityMatchRead]:
    """entity_mapping 行 → 设计稿契约（按统一实体分组 + 打分排序）。

    返回新列表，不改动入参；空输入 ⇒ 空列表。
    """
    if not rows:
        return []
    grouped: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[row.enterprise_key].append(row)
    matches: list[ProcurementEntityMatchRead] = []
    for enterpriseKey, items in grouped.items():
        first = items[0]
        mappings = [
            ProcurementEntitySourceMappingRead(
                source_system=item.source_system,
                source_key=item.source_key,
                source_code=item.source_code,
                match_rule=item.match_rule,
                effective_date=item.effective_date,
                expiry_date=item.expiry_date,
            )
            for item in items
        ]
        matches.append(
            ProcurementEntityMatchRead(
                entity_type=first.entity_type,
                enterprise_key=enterpriseKey,
                enterprise_code=first.enterprise_code,
                name=first.name,
                match_score=max(_rowScore(item, (q or "").strip()) for item in items),
                source_mappings=mappings,
            )
        )
    return sorted(matches, key=lambda m: (-m.match_score, m.enterprise_key))
