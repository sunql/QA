"""B1：L1 计算链路的 FeatureDefinition 解析契约——外键链路优先，formula 等值回退。

真实 PG（Harness 测试规范）。`_resolveCalculationFeature` 是纯解析函数
（session + KpiCatalog 入参），这里直接对 ChatService 实例调用并断言：

1. metric_id 命中 → 走 metric.formula 锚，不进回退路径（无回退 warning）
2. metric 链路未命中 → 降级回退 kpi.formula，命中时 warning 提示补挂外键
3. 无 metric_id 直接回退命中 → warning（脆弱路径可见化——2026-10-27 探针：
   dev 12 条存量 KPI 全走此路径且全部未命中，此前是静默的）
4. 双路径都未命中 → None + 回退未命中 warning
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, FeatureDefinition, KpiCatalog, OntologyMetric
from app.services.chat_service import ChatService

pytestmark = pytest.mark.asyncio

loggerName = "app.services.chat_service"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seedFeature(
    session: AsyncSession, *, code: str, anchor: str, dsId: int = 9801
) -> FeatureDefinition:
    """FD.datasource_id NOT NULL → 先落数据源再落 FD。feature_definition 列是锚文本。"""
    session.add(
        DataSource(
            id=dsId,
            name=f"ds-b1-{dsId}",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="x",
            username="u",
            password_encrypted="x",
        )
    )
    feat = FeatureDefinition(
        feature_name=f"fn-{code}",
        feature_definition=anchor,
        entity_type="SUPPLIER",
        calculation_logic="SELECT 1",
        datasource_id=dsId,
    )
    session.add(feat)
    await session.commit()
    return feat


async def _seedKpi(
    session: AsyncSession,
    code: str,
    *,
    formula: str | None,
    metricId: int | None = None,
) -> KpiCatalog:
    kpi = KpiCatalog(kpi_code=code, kpi_name=code, status="DRAFT", metric_id=metricId)
    if formula is not None:
        kpi.formula = formula
    session.add(kpi)
    await session.commit()
    return kpi


async def _seedMetric(session: AsyncSession, name: str, formula: str) -> OntologyMetric:
    metric = OntologyMetric(metric_name=name, formula=formula)
    session.add(metric)
    await session.commit()
    return metric


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestMetricPathPriority:
    async def test_metric_id_path_hits_without_fallback(
        self, dbSession: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        feat = await _seedFeature(dbSession, code="otd", anchor="METRIC_ANCHOR_A")
        metric = await _seedMetric(dbSession, "B1指标A", "METRIC_ANCHOR_A")
        kpi = await _seedKpi(
            dbSession, "KPI_B1_A", formula="KPI 自己的旧锚", metricId=metric.id
        )

        with caplog.at_level(logging.WARNING, logger=loggerName):
            resolved = await ChatService()._resolveCalculationFeature(kpi, dbSession)

        assert resolved is not None and resolved.id == feat.id
        # 外键链路命中：不应出现任何回退 warning
        assert not [r for r in caplog.records if "回退" in r.message]

    async def test_metric_miss_degrades_to_fallback_hit(
        self, dbSession: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        feat = await _seedFeature(dbSession, code="fpy", anchor="FD_ANCHOR_B")
        metric = await _seedMetric(dbSession, "B1指标B", "没这条 FD 的公式")
        kpi = await _seedKpi(
            dbSession, "KPI_B1_B", formula="FD_ANCHOR_B", metricId=metric.id
        )

        with caplog.at_level(logging.WARNING, logger=loggerName):
            resolved = await ChatService()._resolveCalculationFeature(kpi, dbSession)

        assert resolved is not None and resolved.id == feat.id
        assert any("回退" in r.message for r in caplog.records)

    async def test_blank_metric_formula_degrades_to_fallback(
        self, dbSession: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """metric 行存在但 formula 为空白（历史脏数据）→ 回退仍可用且告警。

        （悬空 metric_id 被 FK 约束在 DB 层排除——外键链路比字符串等值可靠
        正是 B1 的论点；此分支只防 metric.formula 为空的畸形行。）
        """
        feat = await _seedFeature(dbSession, code="ncr", anchor="FD_ANCHOR_C")
        metric = await _seedMetric(dbSession, "B1指标C", "   ")
        kpi = await _seedKpi(
            dbSession, "KPI_B1_C", formula="FD_ANCHOR_C", metricId=metric.id
        )

        with caplog.at_level(logging.WARNING, logger=loggerName):
            resolved = await ChatService()._resolveCalculationFeature(kpi, dbSession)

        assert resolved is not None and resolved.id == feat.id
        assert any("回退" in r.message for r in caplog.records)


class TestFallbackVisibility:
    async def test_fallback_only_hit_warns(
        self, dbSession: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """存量形态（无 metric_id）命中回退路径 → 必须有 warning 可查。"""
        feat = await _seedFeature(dbSession, code="pv", anchor="FD_ANCHOR_D")
        kpi = await _seedKpi(dbSession, "KPI_B1_D", formula="FD_ANCHOR_D")

        with caplog.at_level(logging.WARNING, logger=loggerName):
            resolved = await ChatService()._resolveCalculationFeature(kpi, dbSession)

        assert resolved is not None and resolved.id == feat.id
        assert any("回退路径" in r.message and "KPI_B1_D" in r.message for r in caplog.records)

    async def test_no_match_returns_none_with_warning(
        self, dbSession: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """双路径皆空 → None（执行降级）+ warning（此前为静默）。"""
        await _seedFeature(dbSession, code="inv", anchor="FD_ANCHOR_E")
        kpi = await _seedKpi(dbSession, "KPI_B1_E", formula="根本不存在的锚")

        with caplog.at_level(logging.WARNING, logger=loggerName):
            resolved = await ChatService()._resolveCalculationFeature(kpi, dbSession)

        assert resolved is None
        assert any("未命中" in r.message and "KPI_B1_E" in r.message for r in caplog.records)

    async def test_blank_formula_returns_none_silently(
        self, dbSession: AsyncSession
    ) -> None:
        kpi = await _seedKpi(dbSession, "KPI_B1_F", formula=None)
        resolved = await ChatService()._resolveCalculationFeature(kpi, dbSession)
        assert resolved is None
