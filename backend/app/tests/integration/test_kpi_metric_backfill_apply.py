"""回填脚本落库（真 PostgreSQL + 真 service 链路 + 每例 TRUNCATE）。

**为什么这组测试不可省**：脚本会写 ``ontology_metric`` / ``ontology_metric_source`` /
``kpi_catalog`` 三张表。写错列名、写错 JSONB 键大小写、幂等性破坏 —— 这些在预览
模式下**全都不会暴露**，只在落库后表现为「对账恒 unknown / 闸门恒阻断」。

外部依赖按仓库既有约定处理：
- Neo4j：autouse 记录器 + ``_graphSyncSuppressed`` 双向断言（T15-F）；
- embedding / Milvus：autouse 记录 ``_ensureEmbedding``，断言一次都没被调用（T15-G）。

T15-Q 例外登记：回填脚本**没有 API 端点**（运维脚本），集成测试直接调
``applyPlans``；用「真 PG + 真 service 链路 + 真 ORM 事件 + 每例 TRUNCATE」
替代「完整 HTTP 链路」。
"""

from __future__ import annotations

import argparse

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.infrastructure.neo4j_client as neo4j
from app.domain.models import (
    AuditOutbox,
    DataSource,
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
    SchemaCache,
)
from app.services.kpi_metric_publish_gate import checkKpiPublishGate
from app.services.kpi_metric_reconcile import (
    REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL,
    REASON_GRAIN_MISMATCH,
    REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE,
    VERDICT_MISMATCH,
    VERDICT_PASS,
    GovernanceSpec,
    MeasureSpec,
    MetricTechnique,
    reconcileGovernanceVsTechnique,
)
from app.services.ontology_service import OntologyService
from app.tests._pg_support import resolveTestDatabaseUrl
from scripts.kpi_metric_backfill import PLAN, applyPlans, classColumnIndex, verifyPlan
from scripts.kpi_metric_backfill_apply import _graphSyncSuppressed
from scripts.kpi_metric_backfill_plan import BACKFILL_ACTOR_ID
from scripts.seed_kpi_semantic_index import _runSeed, seedKpiSemanticIndex

_CLASS_TABLES = (
    "DIM_SUPPLIER",
    "DWD_GOODS_RECEIPT_LINE",
    "DWD_PURCHASE_INVOICE",
    "DWD_PURCHASE_INVOICE_LINE",
    "DWD_PURCHASE_ORDER_LINE",
    "DWD_PURCHASE_REQUISITION_LINE",
    "DWD_REQUISITION_ORDER_LINK",
    "DWD_SUPPLIER_PAYMENT",
    "DWD_SUPPLIER_PRICE_LIST",
)

_OWNER = "THBI"

_TABLE_COLUMNS: dict[str, list[str]] = {
    "DWD_GOODS_RECEIPT_LINE": [
        "RECEIPT_DATE",
        "SUPPLIER_CODE",
        "FACILITY_CODE",
        "PO_NO",
        "PO_LINE_NO",
        "REJECTED_QTY",
        "RECEIVED_QTY",
    ],
    "DWD_PURCHASE_ORDER_LINE": [
        "PROMISED_RECEIPT_DATE",
        "SUPPLIER_CODE",
        "FACILITY_CODE",
        "PO_NO",
        "PO_LINE_NO",
        "ORDER_QTY",
        "UNIT_PRICE",
        "SUPPLIER_MATERIAL_CODE",
        "ORDER_DATE",
    ],
    "DWD_PURCHASE_INVOICE": [
        "AMOUNT_EXCL_TAX",
        "SUPPLIER_CODE",
        "INVOICE_DATE",
        "INVOICE_NO",
        "INVOICE_TYPE",
    ],
    "DWD_PURCHASE_INVOICE_LINE": [
        "LINE_AMOUNT_EXCL_TAX",
        "PO_NO",
        "PO_LINE_NO",
        "QUANTITY",
        "UNIT_PRICE_NET",
        "INVOICE_NO",
        "INVOICE_TYPE",
    ],
    "DWD_PURCHASE_REQUISITION_LINE": ["CREATION_DATE", "REQUISITION_LINE_NO"],
    "DWD_REQUISITION_ORDER_LINK": [
        "PO_NO",
        "PO_LINE_NO",
        "REQUISITION_LINE_NO",
        "REQUISITION_NO",
    ],
    "DWD_SUPPLIER_PAYMENT": ["VALUE_DATE", "DUE_DATE", "SUPPLIER_CODE"],
    "DWD_SUPPLIER_PRICE_LIST": [
        "UNIT_PRICE",
        "MATERIAL_CODE",
        "VALID_FROM",
        "VALID_TO",
    ],
    "DIM_SUPPLIER": ["BPSNUM_0"],
}


@pytest.fixture(autouse=True)
def _neutralizeNeo4j(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Neo4j 同步是 best-effort：记录调用并阻断外部写入（T15-F）。

    返回的记录列表供**反向断言**：本次回填不应产生任何图谱写入。
    """
    calls: list[str] = []
    for name in (
        "upsertMetricNode",
        "linkMetricDerivedFrom",
        "reconcileMetricDerivedFrom",
        "deleteNode",
    ):
        monkeypatch.setattr(
            neo4j, name, lambda *a, _n=name, **k: calls.append(_n)
        )
    return calls


@pytest.fixture(autouse=True)
def _spyEmbedding(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Milvus / embedding 的边界探针（T15-G）。

    ``_ensureEmbedding`` 是唯一创建真实 ``EmbeddingService`` 的地方
    ⇒ 它一次都没被调用 ⇒ 没连 Milvus、没调 embedding provider。
    """
    calls: list[str] = []

    def _record(self: OntologyService) -> None:  # noqa: ARG001
        calls.append("ensureEmbedding")

    monkeypatch.setattr(OntologyService, "_ensureEmbedding", _record)
    return calls


def _plan(code: str):
    return next(p for p in PLAN if p.kpiCode == code)


def _tableEntries() -> list[dict]:
    return [
        {
            "table_name": name,
            "owner": _OWNER,
            "columns": [{"column_name": col} for col in cols],
        }
        for name, cols in _TABLE_COLUMNS.items()
    ]


async def _seedClasses(session: AsyncSession) -> dict[str, int]:
    """造出回填用到的本体类（source_table 指向真实物理表）。

    **刻意不指定 id**（T15-C）：id 由 DB 自增，测试全部按 source_table 动态解析。
    """
    for table in _CLASS_TABLES:
        session.add(
            OntologyClass(
                class_name=table,
                class_alias=table,
                source_table=table,
                created_by="test",
            )
        )
    await session.commit()
    rows = (
        await session.execute(
            select(OntologyClass.source_table, OntologyClass.id).where(
                OntologyClass.source_table.in_(_CLASS_TABLES)
            )
        )
    ).all()
    return {table: classId for table, classId in rows}


async def _seedDatasource(session: AsyncSession, name: str = "DS_BACKFILL_TEST") -> int:
    session.add(
        DataSource(
            name=name,
            type="mysql",
            host="h",
            port=3306,
            database_name="d",
            username="u",
            password_encrypted="x",
            is_active=True,
        )
    )
    await session.commit()
    return (
        await session.execute(select(DataSource.id).where(DataSource.name == name))
    ).scalar_one()


async def _seedSchemaCache(
    session: AsyncSession,
    datasourceId: int,
    tables: list[dict] | None = None,
    *,
    schemaName: str = "",
) -> None:
    session.add(
        SchemaCache(
            datasource_id=datasourceId,
            schema_name=schemaName,
            schema_data=tables if tables is not None else _tableEntries(),
            schema_version="test",
        )
    )
    await session.commit()


async def _seedKpi(
    session: AsyncSession,
    code: str,
    datasourceId: int,
    *,
    status: str = "DRAFT",
) -> KpiCatalog:
    session.add(
        KpiCatalog(
            kpi_code=code,
            kpi_name=code,
            business_definition="d",
            formula="f",
            numerator="n",
            denominator="x",
            grain="g",
            unit="u",
            data_source="T",
            owner="o",
            version="1.0",
            status=status,
            datasource_id=datasourceId,
            metric_id=None,
            created_by="u",
        )
    )
    await session.commit()
    kpi = (
        await session.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code == code)
        )
    ).scalar_one()
    return kpi


async def _seedOtd(session: AsyncSession) -> tuple[KpiCatalog, dict[str, int], int]:
    classIds = await _seedClasses(session)
    dsId = await _seedDatasource(session)
    await _seedSchemaCache(session, dsId)
    kpi = await _seedKpi(session, "KPI_SUPPLIER_OTD", dsId)
    return kpi, classIds, dsId


async def test_apply_creates_metric_and_links_kpi(
    dbSession: AsyncSession, _neutralizeNeo4j: list[str], _spyEmbedding: list[str]
) -> None:
    kpi, classIds, _ = await _seedOtd(dbSession)

    summary = await applyPlans(dbSession, [_plan("KPI_SUPPLIER_OTD")])

    assert summary.created == ("KPI_SUPPLIER_OTD",)
    assert summary.updated == ()
    assert summary.degraded == ()
    assert summary.unbound_data_source == ()
    assert summary.reverted_to_draft == ()

    await dbSession.refresh(kpi)
    assert kpi.metric_id is not None
    # T15-L：治理列保持**业务原声明**，不是物理列
    assert kpi.numerator_columns == ["ACTUAL_RECEIPT_DATE"]
    assert kpi.denominator_columns == ["PROMISED_DATE"]
    assert kpi.grain_keys == ["供应商", "工厂", "月"]

    metric = await dbSession.get(OntologyMetric, kpi.metric_id)
    await dbSession.refresh(metric)
    assert metric.status == "DRAFT"
    assert metric.execution_source == "ESCAPE_HATCH"
    assert metric.metric_type == "RATIO"
    assert metric.aggregation_order == "WEIGHTED_RATIO"
    assert metric.owner == BACKFILL_ACTOR_ID
    # 技术粒度是物理列（与治理粒度刻意不同）
    assert metric.grain_keys == ["SUPPLIER_CODE", "FACILITY_CODE"]

    assert metric.numerator_measure is not None
    assert metric.numerator_measure["agg"] == "COUNT"
    assert metric.numerator_measure["expr"] == "RECEIPT_DATE"
    assert metric.numerator_measure["class_id"] == classIds["DWD_GOODS_RECEIPT_LINE"]
    assert metric.numerator_measure["class_alias"] == "DWD_GOODS_RECEIPT_LINE"
    assert metric.denominator_measure is not None
    assert metric.denominator_measure["expr"] == "PROMISED_RECEIPT_DATE"
    assert (
        metric.denominator_measure["class_id"] == classIds["DWD_PURCHASE_ORDER_LINE"]
    )

    sources = (
        await dbSession.execute(
            select(OntologyMetricSource).where(
                OntologyMetricSource.metric_id == kpi.metric_id
            )
        )
    ).scalars().all()
    assert {(s.class_id, s.role) for s in sources} == {
        (classIds["DWD_GOODS_RECEIPT_LINE"], "NUMERATOR"),
        (classIds["DWD_PURCHASE_ORDER_LINE"], "DENOMINATOR"),
    }
    # 反向断言：没有图谱写入、没有 embedding 调用
    assert _neutralizeNeo4j == []
    assert _spyEmbedding == []


async def test_apply_is_idempotent(
    dbSession: AsyncSession, _neutralizeNeo4j: list[str]
) -> None:
    await _seedOtd(dbSession)
    plan = _plan("KPI_SUPPLIER_OTD")

    await applyPlans(dbSession, [plan])
    summary = await applyPlans(dbSession, [plan])

    assert summary.created == ()
    assert summary.updated == ("KPI_SUPPLIER_OTD",)
    metrics = (
        await dbSession.execute(
            select(OntologyMetric).where(OntologyMetric.metric_name == plan.metricName)
        )
    ).scalars().all()
    assert len(metrics) == 1
    sources = (
        await dbSession.execute(
            select(OntologyMetricSource).where(
                OntologyMetricSource.metric_id == metrics[0].id
            )
        )
    ).scalars().all()
    assert len(sources) == 2
    assert _neutralizeNeo4j == []


async def test_apply_degrades_when_column_missing(
    dbSession: AsyncSession, _neutralizeNeo4j: list[str]
) -> None:
    """物理列缺失 ⇒ 清空结构化度量（降级）；治理列**原样保留**（T15-L）。"""
    kpi, _, _ = await _seedOtd(dbSession)
    rows = (await dbSession.execute(select(SchemaCache))).scalars().all()
    for row in rows:
        row.schema_data = [
            {
                **table,
                "columns": [
                    col
                    for col in table["columns"]
                    if not (
                        table["table_name"] == "DWD_PURCHASE_ORDER_LINE"
                        and col["column_name"] == "PROMISED_RECEIPT_DATE"
                    )
                ],
            }
            for table in row.schema_data
        ]
    await dbSession.commit()

    summary = await applyPlans(dbSession, [_plan("KPI_SUPPLIER_OTD")])

    assert summary.degraded == ("KPI_SUPPLIER_OTD",)
    await dbSession.refresh(kpi)
    metric = await dbSession.get(OntologyMetric, kpi.metric_id)
    await dbSession.refresh(metric)
    assert metric.numerator_measure is None
    assert metric.denominator_measure is None
    # 降级不动治理列：业务原声明仍在 ⇒ 层 2 会判 mismatch（不是自己签字）
    assert kpi.numerator_columns == ["ACTUAL_RECEIPT_DATE"]
    assert _neutralizeNeo4j == []


async def test_layer2_reconcile_reports_mismatch_not_pass(dbSession: AsyncSession) -> None:
    """T15-L：脚本产物**必须**让层 2 报 mismatch（闸门据此拦住 PUBLISHED）。

    若这里判 pass，说明脚本把治理列改写成了物理列 —— 那是「自己给自己签字」，
    正是控制器要求避免的那个缺陷。
    """
    kpi, _, _ = await _seedOtd(dbSession)
    await applyPlans(dbSession, [_plan("KPI_SUPPLIER_OTD")])
    await dbSession.refresh(kpi)
    metric = await dbSession.get(OntologyMetric, kpi.metric_id)
    await dbSession.refresh(metric)

    governance = GovernanceSpec(
        numerator_columns=tuple(kpi.numerator_columns or ()),
        denominator_columns=tuple(kpi.denominator_columns or ()),
        grain_keys=tuple(kpi.grain_keys or ()),
    )
    technique = MetricTechnique(
        metric_type=metric.metric_type,
        numerator=MeasureSpec.fromRaw(metric.numerator_measure),
        denominator=MeasureSpec.fromRaw(metric.denominator_measure),
        aggregation_order=metric.aggregation_order,
        grain_keys=tuple(metric.grain_keys or ()),
        formula=metric.formula,
        execution_source=metric.execution_source,
    )
    result = reconcileGovernanceVsTechnique(governance, technique)
    assert result.verdict == VERDICT_MISMATCH, result.reasons()
    assert result.verdict != VERDICT_PASS
    reasons = set(result.reasons())
    assert REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL in reasons
    assert REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE in reasons
    assert REASON_GRAIN_MISMATCH in reasons


async def test_publish_gate_blocks_the_backfilled_kpi(dbSession: AsyncSession) -> None:
    """发布闸门的 reconcile 项必须**不通过**（原因 = 上面的层 2 mismatch）。"""
    kpi, _, _ = await _seedOtd(dbSession)
    await applyPlans(dbSession, [_plan("KPI_SUPPLIER_OTD")])
    await dbSession.refresh(kpi)

    gate = await checkKpiPublishGate(dbSession, kpi)
    assert gate.passed is False
    failed = {item.code for item in gate.items if not item.ok}
    assert "reconcile" in failed
    assert REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL in gate.reasonCodes


async def test_publish_gate_uses_passed_governance_over_stored_values(
    dbSession: AsyncSession,
) -> None:
    """T16-06 回归：``governance=`` 覆盖必须按**传入值**判定，不能退回实体旧值。

    场景：同一请求里既改治理口径又发布。若闸门读实体上尚未提交的旧列，
    就会用旧口径判 pass/fail —— 两个方向都是错的。
    这里先断言「不传覆盖 ⇒ reconcile 不通过」，再断言「传按技术侧填的覆盖 ⇒
    reconcile 通过」，两条缺一不可（只写后者时，若实现恒 pass 也是绿的）。
    """
    kpi, _, _ = await _seedOtd(dbSession)
    await applyPlans(dbSession, [_plan("KPI_SUPPLIER_OTD")])
    await dbSession.refresh(kpi)

    baseline = await checkKpiPublishGate(dbSession, kpi)
    assert "reconcile" in {item.code for item in baseline.items if not item.ok}

    passedIn = GovernanceSpec(
        numerator_columns=("RECEIPT_DATE",),
        denominator_columns=("PROMISED_RECEIPT_DATE",),
        grain_keys=("SUPPLIER_CODE", "FACILITY_CODE"),
    )
    overridden = await checkKpiPublishGate(dbSession, kpi, governance=passedIn)

    assert "reconcile" not in {item.code for item in overridden.items if not item.ok}
    assert overridden.reasonCodes == ()


async def test_apply_skips_missing_kpi(dbSession: AsyncSession) -> None:
    await _seedClasses(dbSession)
    dsId = await _seedDatasource(dbSession)
    await _seedSchemaCache(dbSession, dsId)

    summary = await applyPlans(dbSession, [_plan("KPI_SUPPLIER_OTD")])

    assert summary.skipped == ("KPI_SUPPLIER_OTD",)
    assert summary.created == ()


async def test_apply_skips_when_class_table_unresolved(dbSession: AsyncSession) -> None:
    """本体类按 source_table 解析不到 ⇒ 跳过（T15-C：宁可少写，不挂错类）。"""
    dsId = await _seedDatasource(dbSession)
    await _seedSchemaCache(dbSession, dsId)
    await _seedKpi(dbSession, "KPI_SUPPLIER_OTD", dsId)

    summary = await applyPlans(dbSession, [_plan("KPI_SUPPLIER_OTD")])

    assert summary.skipped == ("KPI_SUPPLIER_OTD",)
    assert any("DWD_GOODS_RECEIPT_LINE" in item for item in summary.unresolved_classes)


async def test_apply_does_not_change_kpi_status(dbSession: AsyncSession) -> None:
    kpi, _, _ = await _seedOtd(dbSession)
    kpi.status = "PUBLISHED"
    await dbSession.commit()

    await applyPlans(dbSession, [_plan("KPI_SUPPLIER_OTD")])

    await dbSession.refresh(kpi)
    assert kpi.status == "PUBLISHED"


async def test_column_index_supports_bare_and_owner_table_keys(
    dbSession: AsyncSession,
) -> None:
    """T15-N：裸表名与 ``OWNER.TABLE`` 两套键都要有（DW owner = THBI）。"""
    await _seedClasses(dbSession)
    dsId = await _seedDatasource(dbSession)
    await _seedSchemaCache(dbSession, dsId)

    index = await classColumnIndex(dbSession)
    expected = frozenset(_TABLE_COLUMNS["DWD_GOODS_RECEIPT_LINE"])
    assert index["DWD_GOODS_RECEIPT_LINE"] == expected
    assert index[f"{_OWNER}.DWD_GOODS_RECEIPT_LINE"] == expected
    assert verifyPlan(_plan("KPI_SUPPLIER_OTD"), index) == ()


async def test_column_index_unions_same_table_across_datasources(
    dbSession: AsyncSession,
) -> None:
    """T15-N：同名表出现在多个数据源 ⇒ 取并集，不后者覆盖前者。"""
    await _seedClasses(dbSession)
    dsA = await _seedDatasource(dbSession, "DS_A")
    dsB = await _seedDatasource(dbSession, "DS_B")
    await _seedSchemaCache(
        dbSession,
        dsA,
        [{"table_name": "DWD_GOODS_RECEIPT_LINE", "columns": [{"column_name": "A_COL"}]}],
    )
    await _seedSchemaCache(
        dbSession,
        dsB,
        [{"table_name": "DWD_GOODS_RECEIPT_LINE", "columns": [{"column_name": "B_COL"}]}],
    )

    index = await classColumnIndex(dbSession)
    assert index["DWD_GOODS_RECEIPT_LINE"] == frozenset({"A_COL", "B_COL"})


async def test_cli_preview_reads_target_and_writes_nothing(
    dbSession: AsyncSession,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 预览路径：显式 DSN → 守卫 → 自建会话 → 两类计数 → **不写库**。"""
    import scripts.kpi_metric_backfill as cliModule

    await _seedOtd(dbSession)
    out = tmp_path / "review.md"
    args = argparse.Namespace(
        apply=False,
        allow_prod_write=False,
        overwrite=False,
        embed=False,
        out=str(out),
        only="KPI_SUPPLIER_OTD",
        database_url=resolveTestDatabaseUrl(),
    )

    assert await cliModule._run(args) == 0

    printed = capsys.readouterr().out
    assert "新建本体指标：1 条" in printed
    assert "匹配到已有指标（默认只填空值；`--overwrite` 才覆盖）：0 条" in printed
    assert "预览模式：未写库" in printed
    assert out.exists()
    metrics = (await dbSession.execute(select(OntologyMetric))).scalars().all()
    assert metrics == []


async def test_default_apply_preserves_manual_edits(dbSession: AsyncSession) -> None:
    """FIX-I1：不带 --overwrite 的二次 apply **不得**回退人工修订。

    场景即评审给的那条：业务方核对 → 手工改公式 / owner / 治理列并发布 →
    之后为别的 KPI 重跑全量 `--apply` ⇒ 人工值必须仍在、created=0、
    plan 之外的 source 行必须仍在。
    """
    kpi, classIds, _ = await _seedOtd(dbSession)
    plan = _plan("KPI_SUPPLIER_OTD")
    await applyPlans(dbSession, [plan])
    await dbSession.refresh(kpi)
    metric = await dbSession.get(OntologyMetric, kpi.metric_id)
    await dbSession.refresh(metric)

    metric.status = "PUBLISHED"
    metric.formula = "SELECT 1 -- manual edit"
    metric.owner = "human-owner"
    kpi.numerator_columns = ["MANUAL_NUM"]
    kpi.grain_keys = ["人工粒度"]
    dbSession.add(
        OntologyMetricSource(
            metric_id=metric.id, class_id=classIds["DIM_SUPPLIER"], role="FILTER"
        )
    )
    await dbSession.commit()

    summary = await applyPlans(dbSession, [plan])

    assert summary.created == ()
    assert summary.updated == ("KPI_SUPPLIER_OTD",)
    assert summary.reverted_to_draft == ()
    await dbSession.refresh(metric)
    await dbSession.refresh(kpi)
    assert metric.status == "PUBLISHED"
    assert metric.formula == "SELECT 1 -- manual edit"
    assert metric.owner == "human-owner"
    assert kpi.numerator_columns == ["MANUAL_NUM"]
    assert kpi.grain_keys == ["人工粒度"]
    sources = (
        await dbSession.execute(
            select(OntologyMetricSource).where(
                OntologyMetricSource.metric_id == metric.id
            )
        )
    ).scalars().all()
    assert (classIds["DIM_SUPPLIER"], "FILTER") in {
        (row.class_id, row.role) for row in sources
    }


async def test_overwrite_reverts_published_metric_to_draft(
    dbSession: AsyncSession,
) -> None:
    """FIX-I1：--overwrite 写 PUBLISHED 指标 ⇒ 全量重写 + 回退 DRAFT + 删额外 source。"""
    kpi, classIds, _ = await _seedOtd(dbSession)
    plan = _plan("KPI_SUPPLIER_OTD")
    await applyPlans(dbSession, [plan])
    await dbSession.refresh(kpi)
    metric = await dbSession.get(OntologyMetric, kpi.metric_id)
    await dbSession.refresh(metric)
    metric.status = "PUBLISHED"
    metric.formula = "SELECT 2 -- manual edit"
    dbSession.add(
        OntologyMetricSource(
            metric_id=metric.id, class_id=classIds["DIM_SUPPLIER"], role="FILTER"
        )
    )
    await dbSession.commit()

    summary = await applyPlans(dbSession, [plan], overwrite=True)

    assert summary.reverted_to_draft == ("KPI_SUPPLIER_OTD",)
    await dbSession.refresh(metric)
    assert metric.status == "DRAFT"
    assert metric.formula == plan.formula
    assert metric.owner == BACKFILL_ACTOR_ID
    sources = (
        await dbSession.execute(
            select(OntologyMetricSource).where(
                OntologyMetricSource.metric_id == metric.id
            )
        )
    ).scalars().all()
    assert {(row.class_id, row.role) for row in sources} == {
        (classIds["DWD_GOODS_RECEIPT_LINE"], "NUMERATOR"),
        (classIds["DWD_PURCHASE_ORDER_LINE"], "DENOMINATOR"),
    }


async def test_seed_cli_dry_run_prints_and_writes_nothing(
    dbSession: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """FIX-I3：seed 的 --dry-run 只打印，不写库。"""
    dsId = await _seedDatasource(dbSession)
    published = await _seedKpi(dbSession, "KPI_SUPPLIER_OTD", dsId, status="PUBLISHED")
    args = argparse.Namespace(
        database_url=resolveTestDatabaseUrl(),
        allow_prod_write=False,
        dry_run=True,
    )

    assert await _runSeed(args) == 0

    printed = capsys.readouterr().out
    assert "预览模式：未写库" in printed
    assert "KPI_SUPPLIER_OTD" in printed
    assert "准时交付" in printed
    await dbSession.refresh(published)
    assert published.semantic_keywords is None


async def test_seed_semantic_index_writes_through_service(dbSession: AsyncSession) -> None:
    """C3：seed 必须走 `KpiCatalogService.updateKpi`（产出 outbox + revision+1）。

    裸 Core UPDATE 也会改值 ⇒ 只断言「关键词变了」是假绿；必须断言 service 的
    连带产物（outbox 行、revision_count）才能证明没绕回裸 UPDATE。
    """
    dsId = await _seedDatasource(dbSession)
    published = await _seedKpi(dbSession, "KPI_SUPPLIER_OTD", dsId, status="PUBLISHED")
    draft = await _seedKpi(dbSession, "KPI_COST_SAVING", dsId, status="DRAFT")

    affected = await seedKpiSemanticIndex(dbSession)

    assert affected == 1
    await dbSession.refresh(published)
    await dbSession.refresh(draft)
    assert published.semantic_keywords is not None
    assert "准时交付" in published.semantic_keywords
    assert published.revision_count == 1
    assert published.status == "PUBLISHED"
    assert draft.semantic_keywords is None
    events = (
        await dbSession.execute(
            select(AuditOutbox.event_type).where(AuditOutbox.entity_id == published.id)
        )
    ).scalars().all()
    assert "kpi_updated" in events


async def test_graph_sync_suppressed_and_restored(
    _neutralizeNeo4j: list[str],
) -> None:
    """T15-F 双向断言：上下文内被吞，上下文外恢复（防「夹具写了没生效」）。"""
    with _graphSyncSuppressed():
        neo4j.upsertMetricNode(
            unified_id="u", name="n", alias=None, formula="f", aggFunction="SUM"
        )
        neo4j.linkMetricDerivedFrom("a", "b")
    assert _neutralizeNeo4j == []

    neo4j.upsertMetricNode(
        unified_id="u", name="n", alias=None, formula="f", aggFunction="SUM"
    )
    assert _neutralizeNeo4j == ["upsertMetricNode"]
