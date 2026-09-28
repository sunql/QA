"""Wiki 业务规则注入器：评分 / 去重 / 预算 / 渲染。

纯函数（除 getBudget 读 system_config 外），便于 TDD 与单测。

下游消费者（Task 5/7）应只依赖本模块暴露的 dataclass 与 WikiInjector 方法，
不要直接耦合 ORM/wikilink row 形态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_config import SystemConfig


@dataclass(frozen=True)
class ScoredOntology:
    type: str  # 'class' | 'property'
    id: int
    recall_score: float


class WikiLinkLike(Protocol):
    """结构化契约：任何含 page_id/chunk_id/ontology_type/ontology_id/weight 的对象。"""

    page_id: str
    chunk_id: str | None
    ontology_type: str
    ontology_id: int
    weight: Decimal


@dataclass
class ScoredChunk:
    page_id: str
    chunk_id: str | None
    text: str
    score: float
    applied_to: list[tuple[str, int]] = field(default_factory=list)

    def withTextTruncated(self, text: str) -> "ScoredChunk":
        return ScoredChunk(
            page_id=self.page_id,
            chunk_id=self.chunk_id,
            text=text,
            score=self.score,
            applied_to=self.applied_to,
        )


@dataclass(frozen=True)
class WikiBudget:
    maxChars: int = 2000
    maxChunks: int = 5
    minRecallScore: float = 0.0


_DEFAULTS = {
    "WIKI_INJECTION_MAX_CHARS": "2000",
    "WIKI_INJECTION_MAX_CHUNKS": "5",
    "WIKI_INJECTION_MIN_RECALL_SCORE": "0.0",
}
_VALID_TYPES = frozenset({"class", "property"})
_META_OVERHEAD = 60  # 每条 chunk 的 meta 行大约字符数
_HEADER_OVERHEAD = 120  ### 业务规则补充... header + footer
_MIN_TRUNCATE_REMAINS = 80


class WikiInjector:
    """纯函数 + 可选 system_config 读取。"""

    @staticmethod
    def collectAndScore(
        recalledOntologies: list[ScoredOntology],
        linkRows: list[WikiLinkLike],
        chunkTexts: dict[tuple[str, str], str],
        budget: WikiBudget,
    ) -> list[ScoredChunk]:
        if not linkRows:
            return []

        # 防御：budget 异常回退 default
        max_chars = budget.maxChars if budget.maxChars > 0 else int(_DEFAULTS["WIKI_INJECTION_MAX_CHARS"])
        max_chunks = budget.maxChunks if budget.maxChunks > 0 else int(_DEFAULTS["WIKI_INJECTION_MAX_CHUNKS"])
        min_recall = budget.minRecallScore if budget.minRecallScore >= 0 else 0.0
        b = WikiBudget(maxChars=max_chars, maxChunks=max_chunks, minRecallScore=min_recall)

        # Step 1: 索引化 recalled
        recallIndex: dict[tuple[str, int], float] = {
            (o.type, o.id): o.recall_score for o in recalledOntologies
        }

        # Step 2: 分组 + 过滤（未召回 / 召回分数过低 / 类型非法）
        groups: dict[tuple[str, str], list[WikiLinkLike]] = {}
        for lnk in linkRows:
            if lnk.ontology_type not in _VALID_TYPES:
                continue
            key_pair = (lnk.ontology_type, lnk.ontology_id)
            if key_pair not in recallIndex:
                continue
            if recallIndex[key_pair] < b.minRecallScore:
                continue
            key = (lnk.page_id, lnk.chunk_id or "")
            groups.setdefault(key, []).append(lnk)

        # Step 3: 评分（weight × recall_score 求和；Decimal 走 str 归一化）
        scored: list[ScoredChunk] = []
        for (page_id, chunk_id), lnks in groups.items():
            score = 0.0
            for lnk in lnks:
                w = Decimal(str(lnk.weight))
                score += float(w) * recallIndex[(lnk.ontology_type, lnk.ontology_id)]
            # 优先匹配 (page, chunk)；缺失则尝试 page-level（chunk_id 为 None）
            text = chunkTexts.get((page_id, chunk_id))
            if not text and not chunk_id:
                text = chunkTexts.get((page_id, ""))
            if not text:
                continue
            scored.append(
                ScoredChunk(
                    page_id=page_id,
                    chunk_id=chunk_id or None,
                    text=text,
                    score=score,
                    applied_to=[(l.ontology_type, l.ontology_id) for l in lnks],
                )
            )

        # Step 4: 排序 + 截断
        scored.sort(key=lambda c: c.score, reverse=True)
        kept: list[ScoredChunk] = []
        used = 0
        for c in scored:
            block_len = len(c.text) + _META_OVERHEAD
            if used + block_len > b.maxChars - _HEADER_OVERHEAD:
                remain = b.maxChars - _HEADER_OVERHEAD - used - _META_OVERHEAD
                if remain > _MIN_TRUNCATE_REMAINS:
                    kept.append(c.withTextTruncated(c.text[:remain] + "…"))
                break
            used += block_len
            kept.append(c)
            if len(kept) >= b.maxChunks:
                break
        return kept

    @staticmethod
    def renderPromptBlock(
        scoredChunks: list[ScoredChunk],
        charBudget: int,
        wikiPageIndex: dict[str, str],
    ) -> str:
        if not scoredChunks:
            return ""
        lines = [
            f"### 业务规则补充（来自 Wiki · 共 {len(scoredChunks)} 条规则）",
        ]
        for i, c in enumerate(scoredChunks, 1):
            tag = f"[wiki:{c.page_id}:{c.chunk_id or ''}]"
            applied = " · ".join(f"{t}={oid}" for (t, oid) in c.applied_to)
            lines.append(f"{i}. {tag} {c.text}")
            lines.append(f"   适用：{applied}")
        # 规则来源
        page_titles = sorted(
            {
                f"wiki:{p}《{wikiPageIndex.get(p, '?')}》"
                for c in scoredChunks
                for p in [c.page_id]
            }
        )
        lines.append("")
        lines.append("[规则来源] " + " / ".join(page_titles))
        lines.append("")
        lines.append("### 重要")
        lines.append("- 上述业务规则可能与 schema 默认口径冲突，请优先遵循 wiki 规则。")
        lines.append("- wiki 规则不覆盖 schema 引用合法性（仍以 ontology_class_id / property_id 为准）。")
        block = "\n".join(lines)
        if len(block) > charBudget:
            block = block[: charBudget - 1] + "…"
        return block

    @staticmethod
    async def getBudget(session: AsyncSession) -> WikiBudget:
        """读 system_config；缺失走 default。"""
        keys = [
            "WIKI_INJECTION_MAX_CHARS",
            "WIKI_INJECTION_MAX_CHUNKS",
            "WIKI_INJECTION_MIN_RECALL_SCORE",
        ]
        stmt = select(SystemConfig).where(SystemConfig.key.in_(keys))
        rows = (await session.execute(stmt)).scalars().all()
        cfg = {r.key: r.value for r in rows}
        try:
            max_chars = int(cfg.get("WIKI_INJECTION_MAX_CHARS", _DEFAULTS["WIKI_INJECTION_MAX_CHARS"]) or 0)
        except (ValueError, TypeError):
            max_chars = int(_DEFAULTS["WIKI_INJECTION_MAX_CHARS"])
        try:
            max_chunks = int(cfg.get("WIKI_INJECTION_MAX_CHUNKS", _DEFAULTS["WIKI_INJECTION_MAX_CHUNKS"]) or 0)
        except (ValueError, TypeError):
            max_chunks = int(_DEFAULTS["WIKI_INJECTION_MAX_CHUNKS"])
        try:
            min_recall = float(
                cfg.get("WIKI_INJECTION_MIN_RECALL_SCORE", _DEFAULTS["WIKI_INJECTION_MIN_RECALL_SCORE"]) or 0
            )
        except (ValueError, TypeError):
            min_recall = float(_DEFAULTS["WIKI_INJECTION_MIN_RECALL_SCORE"])
        return WikiBudget(
            maxChars=max_chars,
            maxChunks=max_chunks,
            minRecallScore=min_recall,
        )