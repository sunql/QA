"""B2：编译期 KPI 治理契约 ↔ 本体指标技术公式 双向一致性对账。

真实 PG（Harness 测试规范）。`_compileSqlMetadata` 在 metric 编译刷新挂靠 KPI
缓存之后执行 reconcile（只读、不阻断编译）：

1. formula 语义：numerator/denominator 列名集合 ⊆ metric.formula 列集合；
   结构解析不可判定（中文业务文本）→ verdict=unknown（不谎称一致）
2. grain：kpi.grain 是否覆盖 metric.dimension_defaults 键（复用 B5 别名表）
3. formula 混用：kpi.formula 与 metric.formula 文本相同 → 概念混用嫌疑

有 findings → audit_log 落一行（entity_type=kpi_metric_reconcile）；
无 findings → 不落条目。对账失败不阻断编译角色。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.kpi_contract_reconcile as kcr_module
from app.domain.models import AuditLog, KpiCatalog, OntologyMetric
from app.services.knowledge_compiler_service import KnowledgeCompilerService

pytestmark = pytest.mark.asyncio

_RECON_ENTITY_TYPE = "kpi_metric_reconcile"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seedMetric(
    session: AsyncSession,
    *,
    name: str,
    formula: str,
    dimensionDefaults: dict | None = None,
) -> OntologyMetric:
    metric = OntologyMetric(
        metric_name=name,
        formula=formula,
        agg_function="SUM",
        dimension_defaults=dimensionDefaults,
        created_by="recon-test",
    )
    session.add(metric)
    await session.commit()
    return metric


async def _seedKpi(
    session: AsyncSession,
    code: str,
    metricId: int,
    *,
    numerator: str | None = None,
    denominator: str | None = None,
    grain: str | None = None,
    formula: str | None = None,
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=code,
        status="PUBLISHED",
        metric_id=metricId,
        numerator=numerator,
        denominator=denominator,
        grain=grain,
        formula=formula,
        created_by="recon-test",
    )
    session.add(kpi)
    await session.commit()
    return kpi


async def _reconEntries(
    session: AsyncSession, metricId: int
) -> list[AuditLog]:
    rows = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == _RECON_ENTITY_TYPE,
                AuditLog.entity_id == metricId,
            )
        )
    ).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# formula 语义检查
# ---------------------------------------------------------------------------


async def test_column_mismatch_writes_audit_entry(dbSession: AsyncSession) -> None:
    """denominator 列不在 metric.formula 列集合内 → mismatch finding + audit 落一行。"""
    metric = await _seedMetric(
        dbSession, name="准时率-mismatch", formula="SUM(received_qty)/SUM(order_qty)"
    )
    await _seedKpi(
        dbSession,
        "KPI_RECON_01",
        metric.id,
        numerator="received_qty",
        denominator="late_qty",
    )
    svc = KnowledgeCompilerService()
    result = await svc._compileSqlMetadata(dbSession, "metric", metric)

    assert result.status == "success"
    rows = await _reconEntries(dbSession, metric.id)
    assert len(rows) == 1
    findings = rows[0].after_json["kpi_findings"]
    assert findings[0]["kpi_code"] == "KPI_RECON_01"
    check = findings[0]["findings"][0]
    assert check["check"] == "formula_semantics"
    assert check["verdict"] == "mismatch"
    assert "late_qty" in check["detail"]


async def test_consistent_contract_no_audit_entry(dbSession: AsyncSession) -> None:
    """列集合包含 + grain 覆盖 + 无混用 → 无 findings，不落 audit 条目。"""
    metric = await _seedMetric(
        dbSession,
        name="准时率-consistent",
        formula="SUM(received_qty)/SUM(order_qty)",
        dimensionDefaults={"month": "ALL"},
    )
    await _seedKpi(
        dbSession,
        "KPI_RECON_02",
        metric.id,
        numerator="received_qty",
        denominator="order_qty",
        grain="按月",
    )
    svc = KnowledgeCompilerService()
    result = await svc._compileSqlMetadata(dbSession, "metric", metric)

    assert result.status == "success"
    assert await _reconEntries(dbSession, metric.id) == []


async def test_chinese_numerator_verdict_unknown(dbSession: AsyncSession) -> None:
    """中文业务文本抽不出列名 → verdict=unknown（不谎称一致，也不谎称不一致）。"""
    metric = await _seedMetric(
        dbSession, name="准时率-unknown", formula="SUM(received_qty)/SUM(order_qty)"
    )
    await _seedKpi(
        dbSession,
        "KPI_RECON_03",
        metric.id,
        numerator="按期到货行数",
        denominator="总到货行数",
    )
    svc = KnowledgeCompilerService()
    await svc._compileSqlMetadata(dbSession, "metric", metric)

    rows = await _reconEntries(dbSession, metric.id)
    assert len(rows) == 1
    check = rows[0].after_json["kpi_findings"][0]["findings"][0]
    assert check["check"] == "formula_semantics"
    assert check["verdict"] == "unknown"


# ---------------------------------------------------------------------------
# grain / 混用检查
# ---------------------------------------------------------------------------


async def test_grain_gap_produces_finding(dbSession: AsyncSession) -> None:
    """grain 未覆盖 dimension_defaults 的键（含中文别名判定）→ check=grain。"""
    metric = await _seedMetric(
        dbSession,
        name="准时率-grain",
        formula="SUM(received_qty)/SUM(order_qty)",
        dimensionDefaults={"supplier": "ALL", "month": "ALL"},
    )
    await _seedKpi(
        dbSession,
        "KPI_RECON_04",
        metric.id,
        numerator="received_qty",
        denominator="order_qty",
        grain="按月",  # 缺 supplier 覆盖
    )
    svc = KnowledgeCompilerService()
    await svc._compileSqlMetadata(dbSession, "metric", metric)

    rows = await _reconEntries(dbSession, metric.id)
    assert len(rows) == 1
    check = rows[0].after_json["kpi_findings"][0]["findings"][0]
    assert check["check"] == "grain"
    assert "supplier" in check["detail"]


async def test_formula_mixing_produces_finding(dbSession: AsyncSession) -> None:
    """kpi.formula 与 metric.formula 文本相同 → 概念混用嫌疑（锚文本语义被污染）。"""
    sqlFormula = "SUM(received_qty)/SUM(order_qty)"
    metric = await _seedMetric(dbSession, name="准时率-mixing", formula=sqlFormula)
    await _seedKpi(
        dbSession,
        "KPI_RECON_05",
        metric.id,
        numerator="received_qty",
        denominator="order_qty",
        formula=sqlFormula,  # 与 metric.formula 相同 → 混用
    )
    svc = KnowledgeCompilerService()
    await svc._compileSqlMetadata(dbSession, "metric", metric)

    rows = await _reconEntries(dbSession, metric.id)
    assert len(rows) == 1
    checks = rows[0].after_json["kpi_findings"][0]["findings"]
    assert any(c["check"] == "formula_mixing" for c in checks)


# ---------------------------------------------------------------------------
# B5 形状 / 只读 / 不阻断
# ---------------------------------------------------------------------------


async def test_draft_kpi_without_governance_fields_no_findings(
    dbSession: AsyncSession,
) -> None:
    """B5 自动户头（治理字段全空）→ 三检查全跳过，零 findings 零条目（无噪声）。"""
    metric = await _seedMetric(
        dbSession,
        name="准时率-draft",
        formula="SUM(received_qty)/SUM(order_qty)",
        dimensionDefaults={"month": "ALL"},
    )
    await _seedKpi(dbSession, "KPI_RECON_06", metric.id)
    svc = KnowledgeCompilerService()
    result = await svc._compileSqlMetadata(dbSession, "metric", metric)

    assert result.status == "success"
    assert await _reconEntries(dbSession, metric.id) == []


async def test_reconcile_failure_does_not_block_compile(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对账自身抛错 → 编译角色仍 success（缓存刷新是主产物，对账是附加工序）。"""
    metric = await _seedMetric(dbSession, name="准时率-fail", formula="SUM(a)/SUM(b)")
    await _seedKpi(
        dbSession, "KPI_RECON_07", metric.id, numerator="a", denominator="b"
    )

    async def boom(session, audit, metricObj, kpiRows):
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(kcr_module, "reconcileKpiContract", boom)
    svc = KnowledgeCompilerService()
    result = await svc._compileSqlMetadata(dbSession, "metric", metric)

    assert result.status == "success"
    assert await _reconEntries(dbSession, metric.id) == []


async def test_reconcile_is_read_only(dbSession: AsyncSession) -> None:
    """对账前后 metric/KPI 行字段不变（只读巡检；audit_log 行除外）。"""
    metric = await _seedMetric(
        dbSession, name="准时率-readonly", formula="SUM(received_qty)/SUM(order_qty)"
    )
    kpi = await _seedKpi(
        dbSession,
        "KPI_RECON_08",
        metric.id,
        numerator="received_qty",
        denominator="late_qty",
    )
    beforeMetric = (metric.formula, metric.metric_name)
    beforeKpi = (kpi.numerator, kpi.denominator, kpi.grain, kpi.status)
    svc = KnowledgeCompilerService()
    await svc._compileSqlMetadata(dbSession, "metric", metric)

    await dbSession.refresh(metric)
    await dbSession.refresh(kpi)
    assert (metric.formula, metric.metric_name) == beforeMetric
    assert (kpi.numerator, kpi.denominator, kpi.grain, kpi.status) == beforeKpi


async def test_class_object_skips_reconcile(dbSession: AsyncSession) -> None:
    """class/property 编译无 SQL Metadata 产物 → 不触发对账（维持 skipped 契约）。"""
    metric = await _seedMetric(dbSession, name="不会被编译的指标", formula="SUM(a)")
    svc = KnowledgeCompilerService()
    result = await svc._compileSqlMetadata(dbSession, "class", metric)

    assert result.status == "skipped"
    assert await _reconEntries(dbSession, metric.id) == []
