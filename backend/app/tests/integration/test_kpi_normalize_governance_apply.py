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
    """复核抛错 ⇒ 已执行的 UPDATE 全部回滚。"""

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


async def test_apply_rolls_back_every_plan_when_verify_reports_mismatch(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**真实的**复核 mismatch 分支：第二条失败 ⇒ 第一条的写入也回滚。"""

    class _Mismatch:
        verdict = "mismatch"

        def reasons(self):
            return ("模拟落库后对账 mismatch",)

    monkeypatch.setattr(
        normalizeScript, "reconcileMetric", lambda governance, technique: _Mismatch()
    )
    metricA, kpiA = await _seedRatioKpi(dbSession, "KPI_D3_VERIFY_A")
    metricB, kpiB = await _seedRatioKpi(dbSession, "KPI_D3_VERIFY_B")
    kpiAId, metricAId = kpiA.id, metricA.id
    kpiBId, metricBId = kpiB.id, metricB.id
    plans = [
        deriveNormalization(kpiA, metricA, _CLASS_COLUMNS),
        deriveNormalization(kpiB, metricB, _CLASS_COLUMNS),
    ]

    with pytest.raises(NormalizationError, match="落库后对账仍 mismatch"):
        await normalizeScript.applyPlans(dbSession, plans)

    for kpiId, metricId in ((kpiAId, metricAId), (kpiBId, metricBId)):
        reloaded, _metric = await _reload(dbSession, kpiId, metricId)
        assert reloaded.numerator_columns == ["按期到货行数"], kpiId
        assert reloaded.denominator_columns == ["总到货行数"], kpiId


async def test_apply_rejects_verdict_downgrade(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """计划写后是 pass，复核却回落 unknown ⇒ 视为失败（不是「不是 mismatch 就行」）。"""

    class _Downgraded:
        verdict = "unknown"

        def reasons(self):
            return ()

    monkeypatch.setattr(
        normalizeScript, "reconcileMetric", lambda governance, technique: _Downgraded()
    )
    metric, kpi = await _seedRatioKpi(dbSession, "KPI_D3_DOWNGRADE")
    kpiId, metricId = kpi.id, metric.id
    plan = deriveNormalization(kpi, metric, _CLASS_COLUMNS)
    assert plan.verdict_after == "pass"

    with pytest.raises(NormalizationError, match="verdict"):
        await normalizeScript.applyPlans(dbSession, [plan])

    reloaded, _metric = await _reload(dbSession, kpiId, metricId)
    assert reloaded.numerator_columns == ["按期到货行数"]


async def test_apply_handles_multi_key_grain_in_author_order(
    dbSession: AsyncSession,
) -> None:
    """grain 多键且**非字母序**：落库与复核都不得因为「排序」而误报失败。"""
    metric = OntologyMetric(
        metric_name="KPI_D3_GRAIN_ORDER-metric",
        formula=_FORMULA,
        agg_function="SUM",
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
        execution_source="ESCAPE_HATCH",
        numerator_measure={"agg": "COUNT", "expr": "RECEIPT_DATE"},
        denominator_measure={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
        grain_keys=["SUPPLIER_CODE", "PROVINCE"],
        created_by="d3-test",
    )
    dbSession.add(metric)
    await dbSession.commit()
    kpi = KpiCatalog(
        kpi_code="KPI_D3_GRAIN_ORDER",
        kpi_name="粒度顺序",
        status="PUBLISHED",
        metric_id=metric.id,
        numerator_columns=["按期到货行数"],
        denominator_columns=["总到货行数"],
        grain_keys=["供应商", "省"],
    )
    dbSession.add(kpi)
    await dbSession.commit()
    kpiId, metricId = kpi.id, metric.id
    plan = deriveNormalization(kpi, metric, _CLASS_COLUMNS)
    assert plan.grain_keys == ("SUPPLIER_CODE", "PROVINCE")

    assert await normalizeScript.applyPlans(dbSession, [plan]) == 1

    reloaded, _metric = await _reload(dbSession, kpiId, metricId)
    assert reloaded.grain_keys == ["SUPPLIER_CODE", "PROVINCE"]


async def test_apply_refuses_when_metric_mapping_changed(
    dbSession: AsyncSession,
) -> None:
    """计划里的 metric_id 与库里当前挂靠不一致 ⇒ 拒绝写库（防并发改挂靠）。"""
    metric, kpi = await _seedRatioKpi(dbSession, "KPI_D3_REMAP")
    other = OntologyMetric(
        metric_name="KPI_D3_REMAP-other",
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
    dbSession.add(other)
    await dbSession.commit()
    kpiId, metricId, otherId = kpi.id, metric.id, other.id
    plan = deriveNormalization(kpi, metric, _CLASS_COLUMNS)
    # 模拟快照之后有人把 KPI 改挂到另一个 metric。
    kpi.metric_id = otherId
    await dbSession.commit()

    with pytest.raises(NormalizationError, match="metric_id"):
        await normalizeScript.applyPlans(dbSession, [plan])

    reloaded, _metric = await _reload(dbSession, kpiId, metricId)
    assert reloaded.numerator_columns == ["按期到货行数"]


async def test_apply_rejects_duplicate_metric_in_plan(dbSession: AsyncSession) -> None:
    """同一 metric 被两条计划写 ⇒ 拒绝（后写覆盖先写，等于静默丢一条意图）。"""
    metric, kpiA = await _seedRatioKpi(dbSession, "KPI_D3_DUP_A")
    kpiB = KpiCatalog(
        kpi_code="KPI_D3_DUP_B",
        kpi_name="重复挂靠",
        status="PUBLISHED",
        metric_id=metric.id,
        numerator_columns=["按期到货行数"],
        denominator_columns=["总到货行数"],
        grain_keys=["供应商"],
    )
    dbSession.add(kpiB)
    await dbSession.commit()
    plans = [
        deriveNormalization(kpiA, metric, _CLASS_COLUMNS),
        deriveNormalization(kpiB, metric, _CLASS_COLUMNS),
    ]

    with pytest.raises(NormalizationError, match="metric_id"):
        await normalizeScript.applyPlans(dbSession, plans)


async def test_apply_refuses_when_governance_columns_changed_after_snapshot(
    dbSession: AsyncSession,
) -> None:
    """计划快照后治理列被并发改动 ⇒ 拒绝写库（不得覆盖别人的改动）。"""
    metric, kpi = await _seedRatioKpi(dbSession, "KPI_D3_STALE_COLUMNS")
    kpiId, metricId = kpi.id, metric.id
    plan = deriveNormalization(kpi, metric, _CLASS_COLUMNS)

    kpi.numerator_columns = ["CONCURRENT_EDIT"]
    await dbSession.commit()

    with pytest.raises(NormalizationError, match="numerator_columns.*快照"):
        await normalizeScript.applyPlans(dbSession, [plan])

    storedKpi, _ = await _reload(dbSession, kpiId, metricId)
    assert storedKpi.numerator_columns == ["CONCURRENT_EDIT"]


async def test_apply_refuses_when_measure_changed_after_snapshot(
    dbSession: AsyncSession,
) -> None:
    """计划快照后度量被并发改动 ⇒ 拒绝写库（派生列已过期，写下去就是错值）。"""
    metric, kpi = await _seedRatioKpi(dbSession, "KPI_D3_STALE_MEASURE")
    kpiId, metricId = kpi.id, metric.id
    plan = deriveNormalization(kpi, metric, _CLASS_COLUMNS)

    metric.numerator_measure = {"agg": "SUM", "expr": "SOME_OTHER_COLUMN"}
    await dbSession.commit()

    with pytest.raises(NormalizationError, match="numerator_measure.*快照"):
        await normalizeScript.applyPlans(dbSession, [plan])

    storedKpi, storedMetric = await _reload(dbSession, kpiId, metricId)
    # 治理列与度量都保持并发改动后的状态（既没被覆盖，也没半截写入）。
    assert storedKpi.numerator_columns == ["按期到货行数"]
    assert storedMetric.numerator_measure == {"agg": "SUM", "expr": "SOME_OTHER_COLUMN"}


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
