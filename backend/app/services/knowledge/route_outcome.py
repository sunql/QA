"""P4 路由结果（一路召回的产出）。

四路**同形**：每路都返回「状态 + 证据 + 警告 + 降级原因」，
⇒ 编排器不需要为每一路写一套分支，调用方也能区分
「查到了 0 条」（`EMPTY`）与「这条路挂了」（`DEGRADED`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.knowledge_schemas import (
    KnowledgeEntityRead,
    KnowledgeEvidenceRead,
)


@dataclass(frozen=True, slots=True)
class RouteOutcome:
    """单路召回的结果。

    Attributes:
        channel: 通道（`GRAPH` / `METRIC` / `RAG` / `SQL`）。
        status: `OK` / `EMPTY` / `DEGRADED` / `PENDING`。
        evidence: 证据条目（可为空元组，**不是** None）。
        entities: 命中的实体（可为空元组）。
        warnings: 本路的告警（可为空元组）。
        degraded_reason: 降级原因（仅 `DEGRADED` 时有值）。
    """

    channel: str
    status: str
    evidence: tuple[KnowledgeEvidenceRead, ...] = ()
    entities: tuple[KnowledgeEntityRead, ...] = ()
    warnings: tuple[str, ...] = ()
    degraded_reason: str | None = None

    @property
    def item_count(self) -> int:
        """命中条数（= 证据条数）。"""
        return len(self.evidence)
