"""知识图谱洞察（Phase 3）：拓扑扫描结果 + LLM 解读。

``wiki_graph_insight`` 一行 = 一条拓扑信号 + 一次 LLM 解读。
key 字段因 insight kind 而异：意外连接用「A 页 ↔ B 页」，桥接节点用
「页 id」，知识缺口用「页 id / 社区 key」。``kind`` 与 ``key`` 共同做
唯一索引，确保同一拓扑重算只覆盖，不堆行。

设计取舍：
- 不存拓扑本身（邻居列表/社区归属）—— 拓扑在「扫描时」现算，社区是
  上一步落库的（knowledge_community），不需要再持久化一层。
- ``explanation`` 由 LLM 产出 + 缓存，下一次同拓扑重算读 cache 跳过 LLM，
  节省成本（机制 1/2 的同类设计）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntFk, BigIntPk, _utcnow
from app.domain.wiki_models import JsonColumn

_INSIGHT_KINDS: tuple[str, ...] = (
    "SURPRISING_CONNECTION",  # 跨社区 / 跨维度已确认关系
    "KNOWLEDGE_GAP",          # 孤立 / 稀疏 / 无维度（不调 LLM，但占同一张表）
    "BRIDGE_NODE",            # 连接 3+ 社区的页
)


class WikiGraphInsight(Base):
    """一条拓扑信号 + LLM 解读缓存。"""

    __tablename__ = "wiki_graph_insight"
    __table_args__ = (
        UniqueConstraint("kind", "key", name="uq_graph_insight_kind_key"),
        Index("ix_graph_insight_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    # 拓扑细节（跨社区边两端页面 id、桥接页邻居社区列表等），前端 hover 用
    payload: Mapped[dict] = mapped_column(JsonColumn, nullable=False, default=dict)
    headline: Mapped[str] = mapped_column(String(300), nullable=False)
    # LLM 解读（knowledege_gap 没有 LLM 解读，为空字符串）
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # LLM 解读来源的模型 id（NULL = 无解读，可能是没 LLM 跑过或跑失败）
    model_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("llm_config.id", ondelete="SET NULL"), nullable=True
    )
    # 解读字数
    explanation_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


__all__ = ["WikiGraphInsight", "_INSIGHT_KINDS"]
