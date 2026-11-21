"""I2-B：证据冲突检测（设计稿 §4.3 硬要求）。

「证据冲突时输出冲突提示与来源时间，**不让模型自行选一个看起来合理的值**」。

判据：
1. **同键跨通道不一致 ⇒ 冲突**：同一 (kind, ref) 被 ≥2 个通道给出**不同内容** ⇒ 出 conflict，
   且**带每个来源的通道 + 发布版本 + 观测时间**（可人工裁决）；
2. **同键同内容 ⇒ 不冲突**（两路渲染一致不是冲突）；
3. **单通道多键 ⇒ 不冲突**（同源内部不构成冲突）；
4. **输出确定**（按 key 排序，可逐字比对）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.knowledge_schemas import KnowledgeEvidenceRead
from app.services.knowledge.conflicts import detectConflicts

_T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _ev(channel: str, ref: str, snippet: str, *, version: str = "v1", kind: str = "CLASS"):
    return KnowledgeEvidenceRead(
        channel=channel,
        kind=kind,
        ref=ref,
        title=ref,
        snippet=snippet,
        release_id=7,
        release_version=version,
        observed_at=_T0,
    )


def test_sameKeyDifferentContentAcrossChannelsIsConflict() -> None:
    evidence = (
        _ev("GRAPH", "DIM_SUPPLIER", "供应商主数据（248 个属性）"),
        _ev("SQL", "DIM_SUPPLIER", "DIM_SUPPLIER 物理 schema（240 列）"),
    )
    conflicts = detectConflicts(evidence)
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.key == "CLASS:DIM_SUPPLIER"
    assert conflict.channels == ("GRAPH", "SQL")
    # 每个来源都带版本与时间（人工可裁决，不许模型自选）
    assert {entry.channel for entry in conflict.entries} == {"GRAPH", "SQL"}
    assert all(entry.release_version == "v1" for entry in conflict.entries)
    assert all(entry.observed_at == _T0 for entry in conflict.entries)


def test_sameKeySameContentIsNotConflict() -> None:
    evidence = (
        _ev("GRAPH", "DIM_SUPPLIER", "供应商主数据"),
        _ev("SQL", "DIM_SUPPLIER", "供应商主数据"),
    )
    assert detectConflicts(evidence) == ()


def test_singleChannelIsNeverConflict() -> None:
    evidence = (
        _ev("GRAPH", "DIM_SUPPLIER", "A"),
        _ev("GRAPH", "DIM_SUPPLIER", "B"),
    )
    assert detectConflicts(evidence) == ()


def test_conflictsAreDeterministicallyOrdered() -> None:
    evidence = (
        _ev("SQL", "Z_CLASS", "z-sql"),
        _ev("GRAPH", "Z_CLASS", "z-graph"),
        _ev("SQL", "A_CLASS", "a-sql"),
        _ev("GRAPH", "A_CLASS", "a-graph"),
    )
    keys = [c.key for c in detectConflicts(evidence)]
    assert keys == ["CLASS:A_CLASS", "CLASS:Z_CLASS"]
