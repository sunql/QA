"""B3：KPI 治理契约注入 NL2SQL plan 上下文——契约块装配器集成测试。

真实 PG。`buildKpiContractBlock(session, question)` 是装配器：
1. 问句经 KpiSemanticMatchService（KpiMatchCache，只索引 PUBLISHED）匹配候选 KPI，
   按 confidence 降序取 Top-3；
2. 每个候选过 B2 对账门禁：任何 verdict=mismatch 的检查 → 该 KPI 不注入
   （宁可少注入不注入错的）；unknown 允许注入（声明无法被技术公式反驳，
   恰是治理文本唯一可用的场景）；
3. 有挂靠 metric 且门禁通过 → 渲染技术公式行；无 metric → 仅治理字段。

零回归契约：无匹配 / 冷缓存 / 开关关闭 / 空问句 → None。

匹配路径说明：单 KPI 用例走 exact-alias 快车道（问句含 KPI code 大写下划线词，
confidence=1.0，不依赖 Jaccard 阈值——同 test_chat_l1_routing 的既有约定）；
cap 用例走 Jaccard 路径（keywords 短词 + match_threshold 调低）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.kpi_contract_reconcile as kcr_module
from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_contract_reconcile import buildKpiContractBlock

pytestmark = pytest.mark.asyncio


async def _seedMetric(
    session: AsyncSession, *, name: str, formula: str
) -> OntologyMetric:
    metric = OntologyMetric(
        metric_name=name, formula=formula, agg_function="SUM", created_by="b3-test"
    )
    session.add(metric)
    await session.commit()
    return metric


async def _seedKpi(
    session: AsyncSession,
    code: str,
    *,
    name: str | None = None,
    keywords: list[str] | None = None,
    numerator: str | None = None,
    denominator: str | None = None,
    grain: str | None = None,
    unit: str | None = None,
    metricId: int | None = None,
    status: str = "PUBLISHED",
    matchThreshold: str = "0.75",
) -> KpiCatalog:
    from decimal import Decimal

    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=name or code,
        status=status,
        metric_id=metricId,
        numerator=numerator,
        denominator=denominator,
        grain=grain,
        unit=unit,
        semantic_keywords=keywords or [],
        match_threshold=Decimal(matchThreshold),
    )
    session.add(kpi)
    await session.commit()
    return kpi


async def _warmCache(session: AsyncSession) -> None:
    """warmUp（refreshOne 在冷缓存上是 no-op，测试必须显式整库预热）。"""
    cache = get_kpi_match_cache()
    cache.onKpiChanged()
    await cache.warmUp(session)


# ---------------------------------------------------------------------------
# exact-alias 路径（问句含 KPI code）
# ---------------------------------------------------------------------------


async def test_block_contains_governance_fields_and_formula(
    dbSession: AsyncSession,
) -> None:
    """匹配 + 门禁通过 → 契约块含治理五件套与技术公式。"""
    metric = await _seedMetric(
        dbSession, name="准时率-注入", formula="SUM(received_qty)/SUM(order_qty)"
    )
    await _seedKpi(
        dbSession,
        "KPI_B3_01",
        name="供货及时率",
        keywords=["供货"],
        numerator="received_qty",
        denominator="order_qty",
        grain="按月+供应商",
        unit="%",
        metricId=metric.id,
    )
    await _warmCache(dbSession)

    block = await buildKpiContractBlock(dbSession, "KPI_B3_01 这个指标是多少")

    assert block is not None
    assert "供货及时率" in block
    assert "KPI_B3_01" in block
    assert "received_qty" in block and "order_qty" in block
    assert "按月+供应商" in block
    assert "%" in block
    assert "SUM(received_qty)/SUM(order_qty)" in block


async def test_mismatch_kpi_not_injected(dbSession: AsyncSession) -> None:
    """B2 门禁：denominator 列不在 metric.formula 集合 → mismatch → 不注入。"""
    metric = await _seedMetric(
        dbSession, name="准时率-注入mismatch", formula="SUM(received_qty)/SUM(order_qty)"
    )
    await _seedKpi(
        dbSession,
        "KPI_B3_02",
        name="供货及时率甲",
        keywords=["供货"],
        numerator="received_qty",
        denominator="late_qty",  # 不在公式列集合
        metricId=metric.id,
    )
    await _warmCache(dbSession)

    assert await buildKpiContractBlock(dbSession, "KPI_B3_02 是多少") is None


async def test_unknown_verdict_still_injected(dbSession: AsyncSession) -> None:
    """unknown（中文分子分母判不了）≠ mismatch → 允许注入（不谎称不一致）。"""
    metric = await _seedMetric(
        dbSession, name="准时率-注入unknown", formula="SUM(received_qty)/SUM(order_qty)"
    )
    await _seedKpi(
        dbSession,
        "KPI_B3_03",
        name="供货及时率乙",
        keywords=["供货"],
        numerator="按期到货行数",
        denominator="总到货行数",
        grain="按月",
        metricId=metric.id,
    )
    await _warmCache(dbSession)

    block = await buildKpiContractBlock(dbSession, "KPI_B3_03 是多少")

    assert block is not None
    assert "按期到货行数" in block
    assert "SUM(received_qty)/SUM(order_qty)" in block  # 技术公式照注


async def test_no_metric_governance_only(dbSession: AsyncSession) -> None:
    """无挂靠 metric（无 B2 门禁对象）→ 仅治理字段，无技术公式行。"""
    await _seedKpi(
        dbSession,
        "KPI_B3_04",
        name="库存周转率",
        keywords=["库存"],
        numerator="出库数量",
        denominator="平均库存",
        grain="按月",
    )
    await _warmCache(dbSession)

    block = await buildKpiContractBlock(dbSession, "KPI_B3_04 怎么样")

    assert block is not None
    assert "库存周转率" in block
    assert "技术公式" not in block


# ---------------------------------------------------------------------------
# Jaccard 路径（keywords 短词 + 低阈值）
# ---------------------------------------------------------------------------


async def test_cap_three_kpis(dbSession: AsyncSession) -> None:
    """4 个同义命中 KPI → 只注入 confidence Top-3。"""
    for i in range(4):
        await _seedKpi(
            dbSession,
            f"KPI_B3_CAP{i}",
            name=f"采购额指标{i}",
            keywords=["采购", "额度"],
            numerator="qty",
            matchThreshold="0.05",
        )
    await _warmCache(dbSession)

    block = await buildKpiContractBlock(dbSession, "查采购额度")

    assert block is not None
    assert block.count("- 采购额指标") == 3


# ---------------------------------------------------------------------------
# 零回归契约
# ---------------------------------------------------------------------------


async def test_no_match_returns_none(dbSession: AsyncSession) -> None:
    """问句无任何 KPI 命中 → None（零回归：prompt 与现状完全一致）。"""
    await _seedKpi(
        dbSession, "KPI_B3_05", name="供货及时率丙", keywords=["供货"]
    )
    await _warmCache(dbSession)
    assert await buildKpiContractBlock(dbSession, "今天天气如何") is None


async def test_draft_kpi_not_injected(dbSession: AsyncSession) -> None:
    """DRAFT 不进匹配缓存（warmUp 只收 PUBLISHED）→ 不会被注入。"""
    await _seedKpi(
        dbSession,
        "KPI_B3_06",
        name="草稿指标",
        keywords=["草稿"],
        status="DRAFT",
    )
    await _warmCache(dbSession)
    assert await buildKpiContractBlock(dbSession, "KPI_B3_06 是多少") is None


async def test_flag_disabled_returns_none(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """灰度开关关闭 → None（装配器不工作，prompt 侧零改动）。"""

    class _FakeSettings:
        kpiContractInjectionEnabled = False

    monkeypatch.setattr(kcr_module, "getSettings", lambda: _FakeSettings())
    await _seedKpi(
        dbSession, "KPI_B3_07", name="开关指标", keywords=["开关"]
    )
    await _warmCache(dbSession)
    assert await buildKpiContractBlock(dbSession, "KPI_B3_07 是多少") is None


async def test_empty_question_returns_none(dbSession: AsyncSession) -> None:
    """空问句防御 → None。"""
    assert await buildKpiContractBlock(dbSession, "  ") is None
