"""P3 D5：``kpi_backfill_time_grain`` 幂等回填（真实 PostgreSQL，白名单测试库）。

脚本没有 API 端点（运维工具），按仓库既有例外登记（见
``test_kpi_metric_backfill_apply.py`` 的 T15-Q 说明）直接调 ``_main``：真 PG +
真 ORM + 每例 TRUNCATE，不 mock 持久化层（只 mock「0143 列是否存在」这一探测，
用于钉住未迁移库的优雅降级路径）。

RED 覆盖：
1. 两字段回填：grain 含「月」+ formula 引用唯一日期列 ⇒ time_grain 双写、
   time_column 落 ontology_metric；grain 无时间词 ⇒ time_grain 保持 null；
2. 默认 dry-run 不写库；
3. 幂等：二次 ``--apply`` 变更 0；
4. R3 不猜：多候选 ⇒ 不写 time_column，报告列出候选；grain_keys 唯一日期 token
   优先于 measure/formula 引用；
5. 守卫：``--apply`` 写 ``qa_metadata`` 缺 ``--allow-prod-write`` ⇒ 拒绝（不建连接）；
6. 0143 未迁移：dry-run 打印 ``[schema-not-migrated]`` 且不崩；``--apply`` 拒绝；
7. 非列标识符（日期命名的表 / CTE 名 / 未建模列）不得进入 time_column 候选（Fix round 1 I1）；
8. 已有显式值：与推导同值 ⇒ 0 变更；冲突 ⇒ 保留人工值、绝不覆盖（Fix round 1 I2）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import scripts.kpi_backfill_time_grain as backfillScript
from app.domain.models import (
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
    OntologyProperty,
)
from app.tests._pg_support import resolveTestDatabaseUrl

pytestmark = pytest.mark.asyncio


# ── 造数 ─────────────────────────────────────────────────────────────


async def _seedClass(
    session: AsyncSession,
    className: str,
    properties: list[tuple[str, str, str]],
) -> int:
    """造本体类 + 属性；``properties`` = [(property_name, source_column, data_type)]。"""
    ontologyClass = OntologyClass(
        class_name=className,
        class_alias=className,
        source_table=className,
        created_by="d5-test",
    )
    session.add(ontologyClass)
    await session.commit()
    session.add_all(
        [
            OntologyProperty(
                class_id=ontologyClass.id,
                property_name=name,
                source_column=column,
                data_type=dataType,
                is_primary_key=False,
                is_foreign_key=False,
            )
            for name, column, dataType in properties
        ]
    )
    await session.commit()
    return ontologyClass.id


async def _seedMetric(
    session: AsyncSession,
    name: str,
    *,
    formula: str,
    numeratorMeasure: dict | None,
    denominatorMeasure: dict | None = None,
    grainKeys: list[str] | None = None,
    classId: int | None = None,
    timeGrain: str | None = None,
    timeColumn: str | None = None,
) -> OntologyMetric:
    metric = OntologyMetric(
        metric_name=name,
        formula=formula,
        agg_function="SUM",
        metric_type="MEASURE",
        execution_source="COMPILED",
        numerator_measure=numeratorMeasure,
        denominator_measure=denominatorMeasure,
        grain_keys=grainKeys,
        time_grain=timeGrain,
        time_column=timeColumn,
        created_by="d5-test",
    )
    session.add(metric)
    await session.commit()
    if classId is not None:
        session.add(
            OntologyMetricSource(
                metric_id=metric.id, class_id=classId, role="NUMERATOR"
            )
        )
        await session.commit()
    return metric


async def _seedKpi(
    session: AsyncSession,
    code: str,
    metric: OntologyMetric | None,
    *,
    grain: str | None,
    grainKeys: list[str] | None = None,
    timeGrain: str | None = None,
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=code,
        status="DRAFT",
        metric_id=None if metric is None else metric.id,
        grain=grain,
        grain_keys=grainKeys,
        time_grain=timeGrain,
        numerator_columns=[],
        denominator_columns=[],
        created_by="d5-test",
    )
    session.add(kpi)
    await session.commit()
    return kpi


async def _seedMonthlyKpiWithUniqueDate(session: AsyncSession) -> tuple[KpiCatalog, OntologyMetric]:
    """grain 含「月」；formula 只引用一个日期列 RECEIPT_DATE ⇒ 两字段都应回填。"""
    classId = await _seedClass(
        session,
        "DWD_GOODS_RECEIPT_LINE_D5",
        [
            ("RECEIPT_DATE", "RECEIPT_DATE", "DATE"),
            ("RECEIVED_QTY", "RECEIVED_QTY", "DECIMAL"),
            ("SUPPLIER_CODE", "SUPPLIER_CODE", "VARCHAR"),
            ("ETL_LOAD_TS", "ETL_LOAD_TS", "TIMESTAMP"),
        ],
    )
    metric = await _seedMetric(
        session,
        "D5_RECEIPT-metric",
        formula="SELECT SUM(RECEIVED_QTY) FROM DWD_GOODS_RECEIPT_LINE_D5 WHERE RECEIPT_DATE IS NOT NULL",
        numeratorMeasure={"agg": "SUM", "expr": "RECEIVED_QTY"},
        grainKeys=["SUPPLIER_CODE"],
        classId=classId,
    )
    kpi = await _seedKpi(
        session,
        "KPI_D5_MONTHLY",
        metric,
        grain="供应商+工厂+月",
        grainKeys=["SUPPLIER_CODE"],
    )
    return kpi, metric


async def _seedNoGrainWordKpi(session: AsyncSession) -> tuple[KpiCatalog, OntologyMetric]:
    """grain 无时间词 ⇒ time_grain 不得写入；formula 无日期列 ⇒ time_column 不写。"""
    metric = await _seedMetric(
        session,
        "D5_SNAPSHOT-metric",
        formula="SELECT SUM(AMOUNT) FROM D5_SNAPSHOT_T",
        numeratorMeasure={"agg": "SUM", "expr": "AMOUNT"},
        grainKeys=["SUPPLIER_CODE"],
    )
    kpi = await _seedKpi(
        session,
        "KPI_D5_NO_GRAIN",
        metric,
        grain="供应商（快照）",
        grainKeys=["SUPPLIER_CODE"],
    )
    return kpi, metric


# ── 读回 ─────────────────────────────────────────────────────────────


async def _reloadKpi(session: AsyncSession, kpiCode: str) -> KpiCatalog:
    # 不做 expire_all：两个 helper 连续调用时，后一次的 expire_all 会把前一次
    # 拿到的对象再次标记为 expired ⇒ 同步属性访问触发 lazy load（MissingGreenlet）。
    # populate_existing 已强制从库刷新，足够。
    return (
        await session.execute(
            select(KpiCatalog)
            .where(KpiCatalog.kpi_code == kpiCode)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _reloadMetric(session: AsyncSession, metricId: int) -> OntologyMetric:
    return (
        await session.execute(
            select(OntologyMetric)
            .where(OntologyMetric.id == metricId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# ── ① 两字段回填 + 无时间词不写 ─────────────────────────────────────


async def test_apply_backfills_both_fields_and_skips_no_grain_word(
    dbSession: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    monthlyKpi, monthlyMetric = await _seedMonthlyKpiWithUniqueDate(dbSession)
    noGrainKpi, noGrainMetric = await _seedNoGrainWordKpi(dbSession)

    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 0
    )
    printed = capsys.readouterr().out
    # 无时间词的 KPI 没有任何可写字段 ⇒ 只有 1 条变更
    assert "applied：1 条变更" in printed

    kpi = await _reloadKpi(dbSession, monthlyKpi.kpi_code)
    assert kpi.time_grain == "month"
    metric = await _reloadMetric(dbSession, monthlyMetric.id)
    assert metric.time_grain == "month"
    assert metric.time_column == "RECEIPT_DATE"

    noGrain = await _reloadKpi(dbSession, noGrainKpi.kpi_code)
    assert noGrain.time_grain is None
    noGrainMetricRow = await _reloadMetric(dbSession, noGrainMetric.id)
    assert noGrainMetricRow.time_grain is None
    assert noGrainMetricRow.time_column is None


# ── ② 默认 dry-run 不写库 ────────────────────────────────────────────


async def test_dry_run_reports_and_writes_nothing(
    dbSession: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    kpi, metric = await _seedMonthlyKpiWithUniqueDate(dbSession)

    assert (
        await backfillScript._main(["--database-url", resolveTestDatabaseUrl()]) == 0
    )
    printed = capsys.readouterr().out
    assert "dry-run" in printed
    assert "month" in printed

    kpiRow = await _reloadKpi(dbSession, kpi.kpi_code)
    metricRow = await _reloadMetric(dbSession, metric.id)
    assert kpiRow.time_grain is None
    assert metricRow.time_grain is None
    assert metricRow.time_column is None


# ── ③ 幂等 ───────────────────────────────────────────────────────────


async def test_second_apply_is_noop(
    dbSession: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    await _seedMonthlyKpiWithUniqueDate(dbSession)

    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "applied：0 条变更" in printed
    assert "noop" in printed


# ── ④ R3 不猜：多候选不写 + 报告候选 ─────────────────────────────────


async def test_time_column_ambiguous_candidates_not_written(
    dbSession: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    classId = await _seedClass(
        dbSession,
        "DWD_PURCHASE_ORDER_LINE_D5",
        [
            ("ORDER_DATE", "ORDER_DATE", "DATE"),
            ("PROMISED_RECEIPT_DATE", "PROMISED_RECEIPT_DATE", "DATE"),
            ("ORDER_QTY", "ORDER_QTY", "DECIMAL"),
        ],
    )
    metric = await _seedMetric(
        dbSession,
        "D5_AMBIGUOUS-metric",
        formula=(
            "SELECT SUM(ORDER_QTY) FROM DWD_PURCHASE_ORDER_LINE_D5 "
            "WHERE ORDER_DATE <= PROMISED_RECEIPT_DATE"
        ),
        numeratorMeasure={"agg": "SUM", "expr": "ORDER_QTY"},
        grainKeys=["SUPPLIER_CODE"],
        classId=classId,
    )
    kpi = await _seedKpi(
        dbSession,
        "KPI_D5_AMBIGUOUS",
        metric,
        grain="供应商+月",
        grainKeys=["SUPPLIER_CODE"],
    )

    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "ORDER_DATE" in printed
    assert "PROMISED_RECEIPT_DATE" in printed

    metricRow = await _reloadMetric(dbSession, metric.id)
    assert metricRow.time_grain == "month"
    assert metricRow.time_column is None
    kpiRow = await _reloadKpi(dbSession, kpi.kpi_code)
    assert kpiRow.time_grain == "month"


async def test_time_column_prefers_unique_grain_key_date(
    dbSession: AsyncSession,
) -> None:
    """R3 ①：grain_keys 中的唯一日期 token 优先（即使 formula 还引用了别的日期列）。"""
    classId = await _seedClass(
        dbSession,
        "DWD_PURCHASE_INVOICE_D5",
        [
            ("INVOICE_DATE", "INVOICE_DATE", "DATE"),
            ("BOOKED_DATE", "BOOKED_DATE", "DATE"),
            ("AMOUNT_EXCL_TAX", "AMOUNT_EXCL_TAX", "DECIMAL"),
        ],
    )
    metric = await _seedMetric(
        dbSession,
        "D5_GRAIN_KEY-metric",
        formula=(
            "SELECT SUM(AMOUNT_EXCL_TAX) FROM DWD_PURCHASE_INVOICE_D5 "
            "WHERE BOOKED_DATE IS NOT NULL"
        ),
        numeratorMeasure={"agg": "SUM", "expr": "AMOUNT_EXCL_TAX"},
        grainKeys=["SUPPLIER_CODE"],
        classId=classId,
    )
    await _seedKpi(
        dbSession,
        "KPI_D5_GRAIN_KEY",
        metric,
        grain="供应商+月",
        grainKeys=["INVOICE_DATE"],
    )

    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 0
    )
    metricRow = await _reloadMetric(dbSession, metric.id)
    assert metricRow.time_column == "INVOICE_DATE"


# ── ④b 非列标识符不得成为 time_column 候选（Fix round 1 / I1） ──────


async def test_date_named_table_is_not_a_time_column_candidate(
    dbSession: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """formula 里的 ``FROM DWD_DATE_DIM`` 是表名，不是物理列：不得污染候选集。"""
    classId = await _seedClass(
        dbSession,
        "DWD_PURCHASE_ORDER_LINE_D6",
        [
            ("BOOKED_DATE", "BOOKED_DATE", "DATE"),
            ("ORDER_QTY", "ORDER_QTY", "DECIMAL"),
        ],
    )
    metric = await _seedMetric(
        dbSession,
        "D6_DATE_TABLE-metric",
        formula=(
            "SELECT SUM(ORDER_QTY) FROM DWD_DATE_DIM d "
            "JOIN DWD_PURCHASE_ORDER_LINE_D6 t ON t.DATE_KEY = d.DATE_KEY "
            "WHERE t.BOOKED_DATE IS NOT NULL"
        ),
        numeratorMeasure={"agg": "SUM", "expr": "ORDER_QTY"},
        grainKeys=["SUPPLIER_CODE"],
        classId=classId,
    )
    kpi = await _seedKpi(
        dbSession,
        "KPI_D6_DATE_TABLE",
        metric,
        grain="月",
        grainKeys=["SUPPLIER_CODE"],
    )

    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 0
    )
    printed = capsys.readouterr().out
    # 真实日期列唯一 ⇒ 正常回填；表名 / DATE_KEY 不得出现在候选集
    assert "candidates=['BOOKED_DATE']" in printed
    assert "DWD_DATE_DIM" not in printed
    metricRow = await _reloadMetric(dbSession, metric.id)
    assert metricRow.time_column == "BOOKED_DATE"
    assert (await _reloadKpi(dbSession, kpi.kpi_code)).time_grain == "month"


async def test_cte_name_and_date_named_table_are_not_candidates(
    dbSession: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """CTE 名 ``recent_date`` / 表名 / 未建模的 ``max_date`` 都不是物理列候选。"""
    classId = await _seedClass(
        dbSession,
        "DWD_PURCHASE_ORDER_LINE_D6_CTE",
        [
            ("BOOKED_DATE", "BOOKED_DATE", "DATE"),
            ("ORDER_QTY", "ORDER_QTY", "DECIMAL"),
        ],
    )
    metric = await _seedMetric(
        dbSession,
        "D6_CTE-metric",
        formula=(
            "WITH recent_date AS ("
            "SELECT MAX(ORDER_DATE) AS max_date FROM DWD_DATE_DIM"
            ") SELECT SUM(ORDER_QTY) FROM DWD_PURCHASE_ORDER_LINE_D6_CTE "
            "WHERE BOOKED_DATE >= max_date"
        ),
        numeratorMeasure={"agg": "SUM", "expr": "ORDER_QTY"},
        grainKeys=["SUPPLIER_CODE"],
        classId=classId,
    )
    kpi = await _seedKpi(
        dbSession,
        "KPI_D6_CTE",
        metric,
        grain="月",
        grainKeys=["SUPPLIER_CODE"],
    )

    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "candidates=['BOOKED_DATE']" in printed
    assert "recent_date" not in printed
    assert "max_date" not in printed
    metricRow = await _reloadMetric(dbSession, metric.id)
    assert metricRow.time_column == "BOOKED_DATE"
    assert (await _reloadKpi(dbSession, kpi.kpi_code)).time_grain == "month"


# ── ④c 显式值保留：同值不写、冲突绝不覆盖（Fix round 1 / I2） ────────


async def _seedKpiWithExplicitTimeFields(
    session: AsyncSession,
    *,
    kpiTimeGrain: str | None,
    metricTimeGrain: str | None,
    metricTimeColumn: str | None,
) -> tuple[KpiCatalog, OntologyMetric]:
    """grain=「季度」⇒ 推导 quarter；formula 唯一日期列 = RECEIPT_DATE。"""
    classId = await _seedClass(
        session,
        "DWD_GOODS_RECEIPT_LINE_D6",
        [
            ("RECEIPT_DATE", "RECEIPT_DATE", "DATE"),
            ("BOOKED_DATE", "BOOKED_DATE", "DATE"),
            ("RECEIVED_QTY", "RECEIVED_QTY", "DECIMAL"),
        ],
    )
    metric = await _seedMetric(
        session,
        "D6_EXPLICIT-metric",
        formula=(
            "SELECT SUM(RECEIVED_QTY) FROM DWD_GOODS_RECEIPT_LINE_D6 "
            "WHERE RECEIPT_DATE IS NOT NULL"
        ),
        numeratorMeasure={"agg": "SUM", "expr": "RECEIVED_QTY"},
        grainKeys=["SUPPLIER_CODE"],
        classId=classId,
        timeGrain=metricTimeGrain,
        timeColumn=metricTimeColumn,
    )
    kpi = await _seedKpi(
        session,
        "KPI_D6_EXPLICIT",
        metric,
        grain="季度",
        grainKeys=["SUPPLIER_CODE"],
        timeGrain=kpiTimeGrain,
    )
    return kpi, metric


async def test_existing_equal_values_are_preserved_without_writes(
    dbSession: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """字段已是推导/唯一候选值 ⇒ 保留、0 变更、无冲突 note。"""
    kpi, metric = await _seedKpiWithExplicitTimeFields(
        dbSession,
        kpiTimeGrain="quarter",
        metricTimeGrain="quarter",
        metricTimeColumn="RECEIPT_DATE",
    )

    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "applied：0 条变更" in printed
    assert "保留人工值" not in printed
    assert (await _reloadKpi(dbSession, kpi.kpi_code)).time_grain == "quarter"
    metricRow = await _reloadMetric(dbSession, metric.id)
    assert metricRow.time_grain == "quarter"
    assert metricRow.time_column == "RECEIPT_DATE"


async def test_existing_conflicting_values_are_never_overwritten(
    dbSession: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """显式值与推导冲突 ⇒ 保留人工值，绝不写推导值；三条冲突 note 都出现。"""
    kpi, metric = await _seedKpiWithExplicitTimeFields(
        dbSession,
        kpiTimeGrain="month",
        metricTimeGrain="year",
        metricTimeColumn="BOOKED_DATE",
    )

    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "applied：0 条变更" in printed
    assert (
        "kpi_catalog.time_grain 已有显式值 'month'，与 grain 推导 'quarter' 不同"
        in printed
    )
    assert (
        "ontology_metric.time_grain 已有显式值 'year'，与 grain 推导 'quarter' 不同"
        in printed
    )
    assert (
        "ontology_metric.time_column 已有显式值 'BOOKED_DATE'，"
        "与推导 'RECEIPT_DATE' 不同" in printed
    )
    assert (await _reloadKpi(dbSession, kpi.kpi_code)).time_grain == "month"
    metricRow = await _reloadMetric(dbSession, metric.id)
    assert metricRow.time_grain == "year"
    assert metricRow.time_column == "BOOKED_DATE"


# ── ⑤ 守卫 ───────────────────────────────────────────────────────────


async def test_apply_prod_without_opt_in_is_refused() -> None:
    """``qa_metadata`` + ``--apply`` 缺 ``--allow-prod-write`` ⇒ 拒绝（不建连接）。"""
    with pytest.raises(SystemExit, match="allow-prod-write"):
        await backfillScript._main(
            [
                "--database-url",
                "postgresql+asyncpg://qa_user:x@127.0.0.1:5433/qa_metadata",
                "--apply",
            ]
        )


async def test_apply_refused_on_non_whitelist_db() -> None:
    with pytest.raises(SystemExit, match="不在白名单"):
        await backfillScript._main(
            [
                "--database-url",
                "postgresql+asyncpg://qa_user:x@127.0.0.1:5434/other_db",
                "--apply",
            ]
        )


# ── ⑥ 0143 未迁移：dry-run 报告，apply 拒绝 ──────────────────────────


async def test_dry_run_survives_missing_0143_columns(
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _seedMonthlyKpiWithUniqueDate(dbSession)

    async def _noColumns(session: AsyncSession):
        return backfillScript.SchemaAvailability(
            kpiTimeGrain=False, metricTimeGrain=False, metricTimeColumn=False
        )

    monkeypatch.setattr(backfillScript, "detectSchemaAvailability", _noColumns)

    assert (
        await backfillScript._main(["--database-url", resolveTestDatabaseUrl()]) == 0
    )
    printed = capsys.readouterr().out
    assert "[schema-not-migrated]" in printed
    # 派生值仍然打印（planned），只是不可写
    assert "month" in printed


async def test_apply_refused_when_0143_columns_missing(
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _seedMonthlyKpiWithUniqueDate(dbSession)

    async def _noColumns(session: AsyncSession):
        return backfillScript.SchemaAvailability(
            kpiTimeGrain=False, metricTimeGrain=False, metricTimeColumn=False
        )

    monkeypatch.setattr(backfillScript, "detectSchemaAvailability", _noColumns)

    assert (
        await backfillScript._main(
            ["--database-url", resolveTestDatabaseUrl(), "--apply"]
        )
        == 1
    )
    printed = capsys.readouterr().err
    assert "schema-not-migrated" in printed
