"""冷指标晋升机制（Task 5.1）：扫描 L2 命中 ≥ N 次的问题候选，写入 KpiCatalog（DRAFT）。

数据源：session_message 表（无 kpi_routing_log，按 brief 简化策略执行）。
归一化：LOWER(TRIM(question))。
晋升后 status=DRAFT，需管理员审核才可启用。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, SessionMessage
from app.domain.schemas import KpiCatalogCreate, KpiStatus
from app.services.kpi_catalog_service import KpiCatalogService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(question: str) -> str:
    """归一化 question：去首尾空格 + 转小写 + 去内部空格。"""
    return question.strip().lower().replace(" ", "")


def _extract_keywords(question: str, max_tokens: int = 5) -> list[str]:
    """从 question 提取关键词（用于 KpiCatalog.semantic_keywords）。

    中文（无空格）：按 2-char sliding window 取 bigram；
    英文/数字：按空格/标点拆分，过滤长度<2 的 token。
    返回最多 max_tokens 个。
    """
    words: list[str] = []
    # 英文/数字词：按空格/标点拆分
    english_parts = re.split(r"[\s,，、。！？]+", question)
    for part in english_parts:
        if len(part) >= 2:
            words.append(part)
        elif len(part) == 1 and part.isalpha():
            # 单英文字母忽略（停用词级别）
            pass

    # 中文字符序列：按 bigram sliding window 取词
    chinese_chars = [c for c in question if "一" <= c <= "鿿"]
    for i in range(len(chinese_chars) - 1):
        bigram = chinese_chars[i] + chinese_chars[i + 1]
        if bigram not in words:
            words.append(bigram)

    return words[:max_tokens]


def _candidate_to_code(candidate: PromotionCandidate) -> str:
    """从 candidate 生成唯一 KpiCatalog.kpi_code（幂等）。"""
    return "AUTO_" + hashlib.md5(
        candidate.semantic_key.encode()
    ).hexdigest()[:12].upper()


# ---------------------------------------------------------------------------
# PromotionCandidate（不可变数据）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromotionCandidate:
    """冷指标晋升候选（frozen，不可变）。"""
    semantic_key: str
    hit_count: int
    sample_sql: str | None
    sample_question: str
    first_seen: datetime
    last_seen: datetime


# ---------------------------------------------------------------------------
# MetricPromotionService
# ---------------------------------------------------------------------------

class MetricPromotionService:
    """扫描频繁重复问题并晋升为 KpiCatalog（DRAFT）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def scan_promotion_candidates(
        self,
        *,
        since: datetime,
        min_hits: int = 3,
    ) -> list[PromotionCandidate]:
        """扫描近 N 天 L2 命中 ≥ min_hits 次的候选。

        归一化策略：LOWER(TRIM(question))。
        数据源：session_message 表（仅 assistant 消息）。
        """
        # Step 1: GROUP BY 归一化 question，统计命中次数
        stmt = (
            select(
                func.lower(func.trim(SessionMessage.question)).label("norm_q"),
                func.count(SessionMessage.id).label("hits"),
                func.min(SessionMessage.created_time).label("first_seen"),
                func.max(SessionMessage.created_time).label("last_seen"),
            )
            .where(SessionMessage.created_time >= since)
            .where(SessionMessage.question.isnot(None))
            .group_by(func.lower(func.trim(SessionMessage.question)))
            .having(func.count(SessionMessage.id) >= min_hits)
            .order_by(func.count(SessionMessage.id).desc())
        )
        rows = (await self._session.execute(stmt)).all()

        if not rows:
            return []

        # Step 2: 对每个 norm_q 取一条最新 sample（含 sql_generated）
        candidates: list[PromotionCandidate] = []
        for row in rows:
            norm_q: str = row.norm_q
            sample_row = (
                await self._session.execute(
                    select(SessionMessage)
                    .where(func.lower(func.trim(SessionMessage.question)) == norm_q)
                    .order_by(SessionMessage.created_time.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if sample_row is None:
                continue

            candidates.append(PromotionCandidate(
                semantic_key=_normalize(sample_row.question or ""),
                hit_count=row.hits,
                sample_sql=sample_row.sql_generated,
                sample_question=sample_row.question or "",
                first_seen=row.first_seen,
                last_seen=row.last_seen,
            ))

        return candidates

    async def auto_promote(self, candidate: PromotionCandidate) -> int:
        """把 candidate 写入 KpiCatalog（status=DRAFT）。返回 KpiCatalog.id。

        幂等：若 code 已存在，返回既有 id。
        """
        code = _candidate_to_code(candidate)

        # 幂等检查
        existing = await KpiCatalogService().get_by_code(self._session, code)
        if existing is not None:
            log.info("kpi %s already exists (id=%d), skipping", code, existing.id)
            return existing.id

        # 写入新 KpiCatalog
        dto = KpiCatalogCreate(
            kpi_code=code,
            kpi_name=f"Auto: {candidate.sample_question[:80]}",
            formula=candidate.sample_sql or "",
            status=KpiStatus.DRAFT,
            semantic_keywords=_extract_keywords(candidate.sample_question),
            match_threshold=0.75,
        )

        entity = KpiCatalog(
            kpi_code=dto.kpi_code,
            kpi_name=dto.kpi_name,
            formula=dto.formula,
            status=dto.status.value,
            semantic_keywords=dto.semantic_keywords,
            match_threshold=dto.match_threshold,
        )
        self._session.add(entity)
        await self._session.flush()
        await self._session.commit()
        await self._session.refresh(entity)

        log.info(
            "auto-promote candidate %s -> kpi_catalog id=%d",
            candidate.semantic_key,
            entity.id,
        )
        return entity.id
