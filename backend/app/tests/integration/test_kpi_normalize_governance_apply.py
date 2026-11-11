"""P0 D3：KPI 规范化落库 ``applyPlans``（真实 PostgreSQL）。

脚本没有 API 端点（运维工具），按仓库既有例外登记（见
``test_kpi_metric_backfill_apply.py`` 的 T15-Q 说明）直接调 ``applyPlans``：
真 PG + 真 ORM + 每例 TRUNCATE，不 mock 任何持久化层。

RED 覆盖 5 类：
1. 落库后字段等于目标，且对账 verdict 变为 pass / unknown（不是只看写入成功）；
2. 幂等：重跑 derive 全 noop，``applyPlans`` 返回 0；
3. 目标仍有 mismatch ⇒ 拒绝写库，原值不动；
4. 落库后复核失败（模拟 JSONB 被改写）⇒ 整体回滚，零写入；
5. 非白名单库 / ``qa_metadata`` 未显式 opt-in ⇒ ``SystemExit``（不建连接）。
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import scripts.kpi_normalize_governance as normalizeScript
from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_governance_normalizer import (
    NormalizationError,
    NormalizationPlan,
    deriveNormalization,
)
from app.services.kpi_metric_reconcile import reconcileMetric
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf

pytestmark = pytest.mark.asyncio

# 与 D2 诊断测试同一形状：层 1/2/3 均可 pass。
_FORMULA = (
    "SELECT COUNT(CASE WHEN R.RECEIPT_DATE <= O.PROMISED_RECEIPT_DATE THEN 1 END) "
    "/ NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0) "
    "FROM THBI.DWD_PURCHASE_ORDER_LINE O "
    "JOIN THBI.DWD_GOODS_RECEIPT_LINE R ON O.PO_NO = R.PO_NO"
)

_CLASS_COLUMNS = frozenset({"RECEIPT_DATE", "PROMISED_RECEIPT_DATE", "SUPPLIER_CODE"})


async def _seedRatioKpi(
    session: AsyncSession, code: str
) -> tuple[OntologyMetric, KpiCatalog]:
    """RATIO + 两侧 measure 齐全；治理列先写成业务中文名（apply 前 mismatch）。"""
    metric = OntologyMetric(
        metric_name=f"{code}-metric",
        formula=_FORMULA,
        agg_function="SUM",
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
        execution_source="ESCAPE_HATCH",
        numerator_measure={"agg": "COUNT", "expr": "RECEIPT_DATE"},
        denominator_measure={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
        grain_keys=["SUPPLIER_CODE"],
        created_by="d3-test",
    )
    session.add(metric)
    await session.commit()
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=f"{code} 名称",
        status="PUBLISHED",
        metric_id=metric.id,
        numerator_columns=["按期到货行数"],
        denominator_columns=["总到货行数"],
        grain_keys=["SUPPLIER_CODE"],
    )
    session.add(kpi)
    await session.commit()
    return metric, kpi


async def _seedNoneKpi(session: AsyncSession, code: str) -> tuple[OntologyMetric, KpiCatalog]:
    """NONE 源：规范化后治理字段全清空，对账只能 unknown（Tier C）。"""
    metric = OntologyMetric(
        metric_name=f"{code}-metric",
        formula="",
        agg_function="SUM",
        metric_type="MEASURE",
        execution_source="NONE",
        numerator_measure={"agg": "SUM", "expr": "RECEIPT_DATE"},
        grain_keys=["SUPPLIER_CODE"],
        created_by="d3-test",
    )
    session.add(metric)
    await session.commit()
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=f"{code} 名称",
        status="PUBLISHED",
        metric_id=metric.id,
        numerator_columns=["RECEIPT_DATE"],
        denominator_columns=[],
        grain_keys=["SUPPLIER_CODE"],
    )
    session.add(kpi)
    await session.commit()
    return metric, kpi


async def _reload(
    session: AsyncSession, kpiId: int, metricId: int
) -> tuple[KpiCatalog, OntologyMetric]:
    """读回**库里的值**（``populate_existing`` 覆盖 identity map，避免读到写前缓存）。"""
    kpi = await session.get(KpiCatalog, kpiId, populate_existing=True)
    metric = await session.get(OntologyMetric, metricId, populate_existing=True)
    assert kpi is not None and metric is not None
    return kpi, metric


async def test_apply_writes_target_fields_and_verdicts(
    dbSession: AsyncSession,
) -> None:
    """落库后：治理字段 == 目标；比率型 pass、NONE 源 unknown。"""
    ratioMetric, ratioKpi = await _seedRatioKpi(dbSession, "KPI_D3_RATIO")
    noneMetric, noneKpi = await _seedNoneKpi(dbSession, "KPI_D3_NONE")
    plans = [
        deriveNormalization(ratioKpi, ratioMetric, _CLASS_COLUMNS),
        deriveNormalization(noneKpi, noneMetric, _CLASS_COLUMNS),
    ]
    assert plans[0].verdict_after == "pass"
    assert plans[1].verdict_after == "unknown"

    changed = await normalizeScript.applyPlans(dbSession, plans)
    assert changed == 2

    kpi, metric = await _reload(dbSession, ratioKpi.id, ratioMetric.id)
    assert kpi.numerator_columns == ["RECEIPT_DATE"]
    assert kpi.denominator_columns == ["PROMISED_RECEIPT_DATE"]
    assert kpi.grain_keys == ["SUPPLIER_CODE"]
    assert metric.numerator_measure == {"agg": "COUNT", "expr": "RECEIPT_DATE"}
    assert metric.denominator_measure == {"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"}
    assert reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric)).verdict == "pass"

    kpiNone, metricNone = await _reload(dbSession, noneKpi.id, noneMetric.id)
    assert kpiNone.numerator_columns == []
    assert kpiNone.denominator_columns == []
    assert kpiNone.grain_keys == []
    assert (
        reconcileMetric(governanceSpecOf(kpiNone), techniqueOf(metricNone)).verdict
        == "unknown"
    )


async def test_apply_second_run_is_noop(dbSession: AsyncSession) -> None:
    """幂等：落库后重新 derive 全部 noop，第二次 apply 返回 0。"""
    ratioMetric, ratioKpi = await _seedRatioKpi(dbSession, "KPI_D3_IDEMPOTENT")
    await normalizeScript.applyPlans(
        dbSession, [deriveNormalization(ratioKpi, ratioMetric, _CLASS_COLUMNS)]
    )

    kpi, metric = await _reload(dbSession, ratioKpi.id, ratioMetric.id)
    replan = deriveNormalization(kpi, metric, _CLASS_COLUMNS)
    assert replan.is_noop is True
    assert await normalizeScript.applyPlans(dbSession, [replan]) == 0


async def test_apply_refuses_mismatch_target_and_writes_nothing(
    dbSession: AsyncSession,
) -> None:
    """调用方若已算出 mismatch，apply 必须拒绝（且一行都不写）。"""
    metric, kpi = await _seedRatioKpi(dbSession, "KPI_D3_REFUSE")
    plan = deriveNormalization(kpi, metric, _CLASS_COLUMNS)
    badPlan: NormalizationPlan = dataclasses.replace(plan, verdict_after="mismatch")

    with pytest.raises(NormalizationError, match="拒绝写库"):
        await normalizeScript.applyPlans(dbSession, [badPlan])

    reloaded, _metric = await _reload(dbSession, kpi.id, metric.id)
    assert reloaded.numerator_columns == ["按期到货行数"]
    assert reloaded.denominator_columns == ["总到货行数"]


async def test_apply_rolls_back_when_verification_fails(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复核失败（模拟 JSONB 被改写）⇒ 已执行的 UPDATE 全部回滚。"""

    async def _boom(session: AsyncSession, plans) -> None:
        raise NormalizationError("模拟复核不一致")

    monkeypatch.setattr(normalizeScript, "_verifyApplied", _boom)
    metric, kpi = await _seedRatioKpi(dbSession, "KPI_D3_ROLLBACK")
    # rollback 会 expire 全部属性（含 PK 访问）⇒ id 必须在 apply 前取出。
    kpiId, metricId = kpi.id, metric.id
    plan = deriveNormalization(kpi, metric, _CLASS_COLUMNS)

    with pytest.raises(NormalizationError, match="模拟复核不一致"):
        await normalizeScript.applyPlans(dbSession, [plan])

    reloaded, _metric = await _reload(dbSession, kpiId, metricId)
    assert reloaded.numerator_columns == ["按期到货行数"]
    assert reloaded.denominator_columns == ["总到货行数"]


async def test_main_refuses_non_whitelist_target() -> None:
    """库名不在白名单 ⇒ SystemExit（在建立连接之前就拒绝）。"""
    with pytest.raises(SystemExit, match="不在白名单"):
        await normalizeScript._main(
            ["--database-url", "postgresql+asyncpg://qa_user:x@127.0.0.1:5434/other_db"]
        )


async def test_main_refuses_prod_write_without_opt_in() -> None:
    """``qa_metadata`` + ``--apply`` 未带 ``--allow-prod-write`` ⇒ SystemExit。"""
    with pytest.raises(SystemExit, match="allow-prod-write"):
        await normalizeScript._main(
            [
                "--database-url",
                "postgresql+asyncpg://qa_user:x@127.0.0.1:5433/qa_metadata",
                "--apply",
            ]
        )
