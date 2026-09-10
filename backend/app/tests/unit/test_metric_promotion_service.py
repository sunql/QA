"""Task 5.1: MetricPromotionService 单元测试（TDD RED -> GREEN）。

覆盖：
1. scan_promotion_candidates 找重复问题（按 session_message.question 归一化）
2. min_hits 阈值过滤
3. auto_promote 写入 KpiCatalog（status=DRAFT）
4. 幂等性：重复 promote 返回同一 ID

数据源：session_message 表（无 kpi_routing_log）。
归一化策略：LOWER(TRIM(question))。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.domain.models import KpiCatalog, SessionMessage
from app.domain.schemas import KpiStatus
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.metric_promotion_service import (
    MetricPromotionService,
    PromotionCandidate,
    _candidate_to_code,
    _extract_keywords,
    _normalize,
)


# ---------------------------------------------------------------------------
# Normalize / keyword helpers
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_strips_whitespace(self):
        assert _normalize("  采购订单  ") == "采购订单"

    def test_lowercase(self):
        assert _normalize("采购订单完成率") == "采购订单完成率"

    def test_removes_internal_spaces(self):
        assert _normalize("采购 订单 完成 率") == "采购订单完成率"

    def test_idempotent(self):
        q = "  采购订单  "
        assert _normalize(_normalize(q)) == _normalize(q)


class TestExtractKeywords:
    def test_filters_short_tokens(self):
        # 单字符词被过滤
        keywords = _extract_keywords("供应商 A")
        assert "A" not in keywords

    def test_limits_to_5_tokens(self):
        keywords = _extract_keywords("甲 乙 丙 丁 戊 己 庚 辛")
        assert len(keywords) == 5

    def test_chinese_tokens(self):
        keywords = _extract_keywords("采购订单完成率是多少")
        assert "采购" in keywords
        assert "订单" in keywords


class TestCandidateToCode:
    def test_prefix_auto(self):
        now = datetime.now(timezone.utc)
        candidate = PromotionCandidate(
            semantic_key="采购订单",
            hit_count=3,
            sample_sql="SELECT 1",
            sample_question="采购订单完成率",
            first_seen=now,
            last_seen=now,
        )
        code = _candidate_to_code(candidate)
        assert code.startswith("AUTO_")
        assert len(code) == 5 + 12  # AUTO_ + 12 hex chars

    def test_deterministic(self):
        now = datetime.now(timezone.utc)
        candidate = PromotionCandidate(
            semantic_key="采购订单",
            hit_count=3,
            sample_sql="SELECT 1",
            sample_question="采购订单完成率",
            first_seen=now,
            last_seen=now,
        )
        assert _candidate_to_code(candidate) == _candidate_to_code(candidate)


# ---------------------------------------------------------------------------
# Scan candidates
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def seeded_session_messages(dbSession):
    """种 3 条同 question + 1 条不同 question 到 session_message。"""
    base = datetime.now(timezone.utc) - timedelta(days=5)

    # 3 条相同问题（assistant role，填 sql_generated）
    for i in range(3):
        dbSession.add(SessionMessage(
            session_id=f"sess-{i}",
            role="assistant",
            content="采购订单完成率是多少？",
            question="采购订单完成率",
            sql_generated="SELECT AVG(completion_ratio) FROM purchase_order",
            created_time=base + timedelta(hours=i),
            channel="chat",
        ))

    # 1 条不同问题
    dbSession.add(SessionMessage(
        session_id="sess-x",
        role="assistant",
        content="供应商总数是多少？",
        question="供应商总数",
        sql_generated="SELECT COUNT(*) FROM supplier",
        created_time=base,
        channel="chat",
    ))

    await dbSession.commit()
    return dbSession


@pytest_asyncio.fixture
async def low_hit_session_messages(dbSession):
    """种 2 条同 question（低于 min_hits=3 阈值）。"""
    base = datetime.now(timezone.utc) - timedelta(days=2)

    for i in range(2):
        dbSession.add(SessionMessage(
            session_id=f"sess-low-{i}",
            role="assistant",
            content="在制品库存",
            question="在制品库存",
            sql_generated="SELECT SUM(qty) FROM wip_inventory",
            created_time=base + timedelta(hours=i),
            channel="chat",
        ))

    await dbSession.commit()
    return dbSession


@pytest.mark.asyncio
async def test_scan_finds_repeated_l2_hits(seeded_session_messages):
    """3 次相同问题 → 返回 1 个 candidate，hit_count=3。"""
    service = MetricPromotionService(seeded_session_messages)
    candidates = await service.scan_promotion_candidates(
        since=datetime.now(timezone.utc) - timedelta(days=7),
        min_hits=3,
    )

    assert len(candidates) == 1
    assert candidates[0].hit_count == 3
    assert "采购订单" in candidates[0].sample_question
    assert candidates[0].sample_sql is not None


@pytest.mark.asyncio
async def test_scan_skips_below_threshold(seeded_session_messages):
    """min_hits=5 → 返回空。"""
    service = MetricPromotionService(seeded_session_messages)
    candidates = await service.scan_promotion_candidates(
        since=datetime.now(timezone.utc) - timedelta(days=7),
        min_hits=5,
    )
    assert candidates == []


@pytest.mark.asyncio
async def test_scan_respects_since_filter(seeded_session_messages):
    """只查近 1 天 → 3 次都在 5 天前，被过滤。"""
    service = MetricPromotionService(seeded_session_messages)
    candidates = await service.scan_promotion_candidates(
        since=datetime.now(timezone.utc) - timedelta(days=1),
        min_hits=3,
    )
    assert candidates == []


@pytest.mark.asyncio
async def test_scan_low_hit_returns_nothing(low_hit_session_messages):
    """仅 2 次重复 → 低于阈值 3，返回空。"""
    service = MetricPromotionService(low_hit_session_messages)
    candidates = await service.scan_promotion_candidates(
        since=datetime.now(timezone.utc) - timedelta(days=7),
        min_hits=3,
    )
    assert candidates == []


# ---------------------------------------------------------------------------
# Auto promote
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_promote_writes_kpi_catalog_draft(seeded_session_messages):
    """auto_promote 写 KpiCatalog，status=DRAFT。"""
    service = MetricPromotionService(seeded_session_messages)
    candidates = await service.scan_promotion_candidates(
        since=datetime.now(timezone.utc) - timedelta(days=7),
        min_hits=3,
    )
    assert len(candidates) == 1

    candidate = candidates[0]
    kpi_id = await service.auto_promote(candidate)
    assert kpi_id > 0

    # 验证 KpiCatalog 行
    code = _candidate_to_code(candidate)
    catalog = await KpiCatalogService().get_by_code(seeded_session_messages, code)
    assert catalog is not None
    assert catalog.status == KpiStatus.DRAFT.value
    # semantic_keywords 从 question 提取
    assert catalog.semantic_keywords is not None
    assert len(catalog.semantic_keywords) > 0


@pytest.mark.asyncio
async def test_auto_promote_is_idempotent(seeded_session_messages):
    """重复 promote 同一 candidate 不重复插入（幂等）。"""
    service = MetricPromotionService(seeded_session_messages)
    candidates = await service.scan_promotion_candidates(
        since=datetime.now(timezone.utc) - timedelta(days=7),
        min_hits=3,
    )
    candidate = candidates[0]

    id1 = await service.auto_promote(candidate)
    id2 = await service.auto_promote(candidate)

    assert id1 == id2  # 幂等返回同一 ID


@pytest.mark.asyncio
async def test_auto_promote_uses_candidate_sample_sql(seeded_session_messages):
    """写入的 formula = candidate.sample_sql。"""
    service = MetricPromotionService(seeded_session_messages)
    candidates = await service.scan_promotion_candidates(
        since=datetime.now(timezone.utc) - timedelta(days=7),
        min_hits=3,
    )
    candidate = candidates[0]

    await service.auto_promote(candidate)
    code = _candidate_to_code(candidate)

    catalog = await KpiCatalogService().get_by_code(seeded_session_messages, code)
    assert catalog is not None
    assert "SELECT" in (catalog.formula or "").upper()
