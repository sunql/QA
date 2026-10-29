"""回填脚本的映射表、目标库守卫与校验逻辑（纯函数，不碰库）。

覆盖三组：
1. **映射表性质**（`PLAN` 12 条 / 档位分布 / 治理列与物理列**不相同**）；
2. **物理列校验**（`verifyPlan` 的缺列检出）；
3. **目标库守卫**（T15-W 四道闸 + T15-A「校验的 DSN == 落库的 DSN」）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import ProgrammingError

import scripts.kpi_metric_backfill as cliModule
from app.services.nl2sql_semantic_guard import measureColumnIdentifiers
from scripts.kpi_metric_backfill import (
    PLAN,
    MeasurePlan,
    _assertSameDatabase,
    applyPlans,
    assertSafeTarget,
    buildPlans,
    buildSessionFactory,
    classColumnIndex,
    databaseNameOf,
    maskDsn,
    renderPreview,
    renderReviewChecklist,
    verifyPlan,
)

EXPECTED_TIER_COUNTS = {"A": 4, "B": 5, "C": 3}


def _columnIndex(columns: dict[str, set[str]]) -> dict[str, frozenset[str]]:
    return {k: frozenset(v) for k, v in columns.items()}


# ── 映射表性质 ────────────────────────────────────────────────────────


def test_plan_covers_twelve_kpis_and_has_unique_codes() -> None:
    assert len(PLAN) == 12
    codes = [p.kpiCode for p in PLAN]
    assert len(set(codes)) == 12
    assert all(p.metricName for p in PLAN)
    assert all(p.formula for p in PLAN)  # 每条都有口径声明


def test_buildPlans_returns_the_same_plan_set() -> None:
    """`buildPlans()` 是 PLAN 的唯一访问入口（不是第二份真相）。"""
    assert [p.kpiCode for p in buildPlans()] == [p.kpiCode for p in PLAN]


def test_tier_distribution_is_a4_b5_c3() -> None:
    byTier: dict[str, list[str]] = {}
    for plan in PLAN:
        byTier.setdefault(plan.tier(), []).append(plan.kpiCode)
    assert {tier: len(codes) for tier, codes in byTier.items()} == EXPECTED_TIER_COUNTS


def test_ratio_plans_have_legal_aggregation_order() -> None:
    for plan in PLAN:
        if plan.metricType != "RATIO":
            continue
        assert plan.aggregationOrder in {"WEIGHTED_RATIO", "AVG_OF_RATIOS"}, plan.kpiCode


def test_tier_a_plans_have_both_measures() -> None:
    """A 档（结构可映射）必须分子分母齐全，否则层 2 对账必然 mismatch。"""
    structured = [p for p in PLAN if p.numerator is not None]
    assert len(structured) == 4
    for plan in structured:
        assert plan.denominator is not None, plan.kpiCode
        assert plan.executionSource == "ESCAPE_HATCH", plan.kpiCode


def test_tier_c_plans_have_no_physical_source() -> None:
    """C 档：物理源缺失 ⇒ execution_source = NONE（不可执行）+ 无 source 行。"""
    for code in (
        "KPI_SUPPLIER_DEFECT_RATE",
        "KPI_SUPPLIER_FPY",
        "KPI_SUPPLIER_NCR_RATE",
    ):
        plan = next(p for p in PLAN if p.kpiCode == code)
        assert plan.executionSource == "NONE", code
        assert plan.classRefs == (), code
        assert plan.numerator is None and plan.denominator is None, code


def test_tier_b_maintenance_ratio_has_partial_source() -> None:
    """MAINTENANCE_RATIO：B 档（部分物理源可确认）但公式不可执行 ⇒ NONE。"""
    plan = next(p for p in PLAN if p.kpiCode == "KPI_SUPPLIER_MAINTENANCE_RATIO")
    assert plan.tier() == "B"
    assert plan.executionSource == "NONE"
    assert plan.classRefs == (("DIM_SUPPLIER", "BASE"),)


def test_measure_raw_keys_are_snake_case() -> None:
    raw = MeasurePlan(
        agg="COUNT", expr="RECEIPT_DATE", classTable="DWD_GOODS_RECEIPT_LINE",
        classAlias="DWD_GOODS_RECEIPT_LINE",
    ).toRaw()
    assert raw["agg"] == "COUNT"
    assert raw["expr"] == "RECEIPT_DATE"
    assert raw["class_alias"] == "DWD_GOODS_RECEIPT_LINE"
    assert "classId" not in raw
    assert "classAlias" not in raw


def test_measure_raw_omits_empty_filter() -> None:
    raw = MeasurePlan(
        agg="SUM", expr="AMOUNT", classTable="X", classAlias="X"
    ).toRaw()
    assert "filter" not in raw


def test_measure_raw_has_no_class_id_before_resolution() -> None:
    """class_id 必须落库时按 source_table 解析（T15-C：不硬编码主键）。"""
    raw = MeasurePlan(
        agg="COUNT", expr="X", classTable="T", classAlias="T"
    ).toRaw()
    assert "class_id" not in raw


def test_governance_columns_are_business_original_not_physical() -> None:
    """T15-L：治理列必须是**业务原声明**，不能是物理列（否则层 2 是自己和自己比）。

    判定标准：每条有结构化度量的 plan，治理列里**至少有一个**名字不存在于物理
    度量标识符中（同一列恰好同名不算错，例如付款准时率的 DUE_DATE）。
    """
    for plan in PLAN:
        if plan.numerator is None:
            continue
        physical: set[str] = set()
        for measure in (plan.numerator, plan.denominator):
            if measure is not None:
                physical |= measureColumnIdentifiers(measure.expr)
        declared = {
            col.lower()
            for col in plan.governanceNumerator + plan.governanceDenominator
        }
        assert declared, plan.kpiCode
        assert declared - physical, plan.kpiCode

    otd = next(p for p in PLAN if p.kpiCode == "KPI_SUPPLIER_OTD")
    assert otd.governanceNumerator == ("ACTUAL_RECEIPT_DATE",)
    assert otd.numerator is not None and otd.numerator.expr == "RECEIPT_DATE"
    assert otd.governanceNumerator != (otd.numerator.expr,)


def test_governance_grain_is_business_original() -> None:
    """治理粒度是业务口径的中文声明（供应商+工厂+月），不是物理列名。"""
    plan = next(p for p in PLAN if p.kpiCode == "KPI_SUPPLIER_OTD")
    assert plan.governanceGrain == ("供应商", "工厂", "月")
    assert plan.grainKeys == ("SUPPLIER_CODE", "FACILITY_CODE")


def test_class_refs_use_source_table_not_class_id() -> None:
    """类引用按 source_table 落库（换库 / 换 seed 顺序仍有效）。"""
    for plan in PLAN:
        for tableName, role in plan.classRefs:
            assert isinstance(tableName, str) and tableName
            assert role in {"NUMERATOR", "DENOMINATOR", "FILTER", "BASE"}


def test_formulas_use_oracle_dialect_and_thbi_prefix() -> None:
    """T15-M：公式按 Oracle 方言 + THBI 前缀；不得出现 DATEDIFF。"""
    for plan in PLAN:
        assert "DATEDIFF" not in plan.formula.upper(), plan.kpiCode
        if plan.executionSource == "ESCAPE_HATCH":
            assert "THBI." in plan.formula, plan.kpiCode


def test_join_keys_include_po_no_and_po_line_no() -> None:
    """T15-M：跨订单串联必须按 (po_no, po_line_no) 双键 join。"""
    for code in (
        "KPI_SUPPLIER_OTD",
        "KPI_SUPPLIER_OVERDUE_RATIO",
        "KPI_SUPPLIER_DELAY_DAYS",
    ):
        plan = next(p for p in PLAN if p.kpiCode == code)
        assert "O.PO_NO = R.PO_NO" in plan.formula, code
        assert "O.PO_LINE_NO = R.PO_LINE_NO" in plan.formula, code


def test_preview_and_checklist_mention_every_kpi() -> None:
    preview = renderPreview(PLAN)
    checklist = renderReviewChecklist(PLAN)
    for plan in PLAN:
        assert plan.kpiCode in preview
        assert plan.kpiCode in checklist


def test_checklist_lists_governance_to_physical_mapping() -> None:
    """审核清单必须逐条给出「治理列 ↔ 物理列」映射（T15-L 第 3 点）。"""
    checklist = renderReviewChecklist(PLAN)
    assert "ACTUAL_RECEIPT_DATE" in checklist
    assert "RECEIPT_DATE" in checklist
    assert "PROMISED_RECEIPT_DATE" in checklist


# ── 物理列校验 ────────────────────────────────────────────────────────


def test_verify_plan_detects_missing_column() -> None:
    plan = next(p for p in PLAN if p.kpiCode == "KPI_SUPPLIER_OTD")
    # 收货行表里没有 PROMISED_RECEIPT_DATE（它在订单行表）⇒ 分母列缺失
    index = _columnIndex(
        {
            "DWD_GOODS_RECEIPT_LINE": {"RECEIPT_DATE", "SUPPLIER_CODE"},
            "DWD_PURCHASE_ORDER_LINE": {"SUPPLIER_CODE"},
        }
    )
    missing = verifyPlan(plan, index)
    assert missing == ("DWD_PURCHASE_ORDER_LINE.PROMISED_RECEIPT_DATE",)


def test_verify_plan_passes_when_all_columns_exist() -> None:
    plan = next(p for p in PLAN if p.kpiCode == "KPI_SUPPLIER_OTD")
    index = _columnIndex(
        {
            "DWD_GOODS_RECEIPT_LINE": {"RECEIPT_DATE", "SUPPLIER_CODE", "FACILITY_CODE"},
            "DWD_PURCHASE_ORDER_LINE": {
                "PROMISED_RECEIPT_DATE",
                "SUPPLIER_CODE",
                "FACILITY_CODE",
            },
        }
    )
    assert verifyPlan(plan, index) == ()


def test_verify_plan_accepts_table_keyed_index() -> None:
    """表名键索引同样可用（apply 内部按 source_table 解析后校验）。"""
    plan = next(p for p in PLAN if p.kpiCode == "KPI_SUPPLIER_OTD")
    index = {
        "DWD_GOODS_RECEIPT_LINE": frozenset({"RECEIPT_DATE"}),
        "DWD_PURCHASE_ORDER_LINE": frozenset({"PROMISED_RECEIPT_DATE"}),
    }
    assert verifyPlan(plan, index) == ()


def test_verify_plan_accepts_arithmetic_expression() -> None:
    """T15-O：expr 是算术式时按标识符拆列，不整串比对。

    整串比对会把 ``UNIT_PRICE_NET * QUANTITY`` 当成一个列名 ⇒ 必然误报缺列；
    拆开后两列都在 ⇒ 通过；少一列 ⇒ 只报那一列。
    """
    base = next(p for p in PLAN if p.kpiCode == "KPI_SUPPLIER_OTD")
    arithmetic = MeasurePlan(
        agg="SUM",
        expr="UNIT_PRICE_NET * QUANTITY",
        classTable="DWD_PURCHASE_INVOICE_LINE",
        classAlias="DWD_PURCHASE_INVOICE_LINE",
    )
    plan = replace(base, numerator=arithmetic, denominator=None, classRefs=())
    assert verifyPlan(
        plan, {"DWD_PURCHASE_INVOICE_LINE": frozenset({"UNIT_PRICE_NET", "QUANTITY"})}
    ) == ()
    assert verifyPlan(
        plan, {"DWD_PURCHASE_INVOICE_LINE": frozenset({"UNIT_PRICE_NET"})}
    ) == ("DWD_PURCHASE_INVOICE_LINE.QUANTITY",)


# ── 目标库守卫（T15-W 四道闸）────────────────────────────────────────


def test_safe_target_rejects_unknown_database() -> None:
    with pytest.raises(SystemExit):
        assertSafeTarget("postgresql+asyncpg://u:p@host:5432/qa_system_prod")


def test_safe_target_rejects_production_database_without_opt_in() -> None:
    """真生产库名（与 dev 同名）⇒ 白名单放行，但缺 opt-in ⇒ 必须拒绝。"""
    with pytest.raises(SystemExit):
        assertSafeTarget("postgresql+asyncpg://u:p@host:5432/qa_metadata")


def test_safe_target_accepts_production_database_with_opt_in() -> None:
    assert (
        assertSafeTarget(
            "postgresql+asyncpg://u:p@host:5432/qa_metadata", allowProdWrite=True
        )
        == "qa_metadata"
    )


def test_safe_target_accepts_test_database_without_opt_in() -> None:
    assert (
        assertSafeTarget("postgresql+asyncpg://u:p@host:5432/qa_metadata_test")
        == "qa_metadata_test"
    )


def test_database_name_of_variants() -> None:
    assert databaseNameOf("postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test") == (
        "qa_metadata_test"
    )
    assert databaseNameOf("postgresql+asyncpg://u:p@localhost:5434") == ""
    assert databaseNameOf("not-a-dsn") == ""
    assert (
        databaseNameOf("postgresql+asyncpg://u:p@localhost:5434/db?ssl=require")
        == "db"
    )


def test_mask_dsn_hides_password() -> None:
    masked = maskDsn("postgresql+asyncpg://qa_user:sup3rsecret@localhost:5434/db")
    assert "sup3rsecret" not in masked
    assert "qa_user" in masked
    assert maskDsn("postgresql+asyncpg://localhost:5434/db") == "***"


def test_session_factory_is_bound_to_the_guarded_dsn() -> None:
    """T15-A：落库会话必须由**被守卫放行的那个 DSN** 建出来（不是 settings）。"""
    dsn = "postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test_t15"
    factory = buildSessionFactory(dsn)
    bind = factory.kw["bind"]
    assert bind.url.database == "qa_metadata_test_t15"


def test_apply_plans_is_exported_from_cli_module() -> None:
    """Task 16 会从 `scripts.kpi_metric_backfill` 复用 apply 模块的三个名字。"""
    assert callable(applyPlans)
    assert callable(classColumnIndex)
    assert callable(verifyPlan)


class _FakeResult:
    def __init__(self, databaseName: str) -> None:
        self._databaseName = databaseName

    def scalar_one(self) -> str:
        return self._databaseName


class _FakeSession:
    """只实现 `_assertSameDatabase` 用到的 `execute().scalar_one()`。"""

    def __init__(self, databaseName: str) -> None:
        self._databaseName = databaseName

    async def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult(self._databaseName)


async def test_session_target_assertion_accepts_matching_database() -> None:
    await _assertSameDatabase(_FakeSession("qa_metadata_test_t15"), "qa_metadata_test_t15")


async def test_session_target_assertion_rejects_mismatched_database() -> None:
    """T15-A：守卫放行的库名 ≠ 会话实连库名 ⇒ 必须拒绝落库。"""
    with pytest.raises(RuntimeError, match="不一致"):
        await _assertSameDatabase(_FakeSession("qa_metadata"), "qa_metadata_test_t15")


# ── CLI 纯函数（预览两类计数 / 审核清单 / 备份闸 / 错误提示）────────────


def test_preview_reports_new_and_matched_counts_separately() -> None:
    """dispatch 要求：**新建**与**匹配到已有**分开计数。"""
    context = cliModule.PreviewContext(
        existingMetricNames=frozenset({PLAN[0].metricName}),
        matchedKpiCodes=frozenset({PLAN[1].kpiCode}),
        degraded=((PLAN[2].kpiCode, ("DWD_X.MISSING_COL",)),),
        unresolvedClasses=((PLAN[3].kpiCode, ("DWD_Y（BASE）",)),),
    )
    preview = renderPreview(PLAN, context)
    assert "新建本体指标：10 条" in preview
    assert "匹配到已有指标（只补结构化字段）：2 条" in preview
    assert "物理列缺失（将降级）：DWD_X.MISSING_COL" in preview
    assert "本体类未解析（将跳过）：DWD_Y（BASE）" in preview


def test_preview_without_context_treats_everything_as_new() -> None:
    assert "新建本体指标：12 条" in renderPreview(PLAN)
    assert "匹配到已有指标（只补结构化字段）：0 条" in renderPreview(PLAN)


def test_checklist_states_tier_b_injection_default_on_decision() -> None:
    """审核清单必须写出「Tier B 契约注入默认开」的灰度决策（grep 得到）。"""
    checklist = renderReviewChecklist(PLAN)
    assert "Tier B" in checklist
    assert "KPI_CONTRACT_INJECTION_ENABLED" in checklist
    assert "默认 true" in checklist
    assert "先关后开" in checklist
    assert "KPI_METRIC_GATE_ENABLED" in checklist


def test_checklist_summary_mentions_issues() -> None:
    context = cliModule.PreviewContext(
        degraded=(("KPI_SUPPLIER_OTD", ("DWD_X.C",)),),
        unresolvedClasses=(("KPI_COST_SAVING", ("DWD_Y（BASE）",)),),
    )
    checklist = renderReviewChecklist(PLAN, context)
    assert "物理列缺失将降级：KPI_SUPPLIER_OTD" in checklist
    assert "本体类未解析将跳过：KPI_COST_SAVING" in checklist
    assert "新建本体指标：12 条" in checklist
    assert "匹配到已有指标：0 条" in checklist


def test_require_database_url_rejects_missing_target() -> None:
    with pytest.raises(SystemExit, match="--database-url"):
        cliModule._requireDatabaseUrl(None)
    assert cliModule._requireDatabaseUrl("postgresql://x/y") == "postgresql://x/y"


def test_select_plans_filters_by_kpi_code() -> None:
    assert len(cliModule._selectPlans(None)) == 12
    picked = cliModule._selectPlans(" KPI_SUPPLIER_OTD , KPI_COST_SAVING ")
    assert [p.kpiCode for p in picked] == ["KPI_SUPPLIER_OTD", "KPI_COST_SAVING"]
    assert cliModule._selectPlans("NOPE") == []


def test_tier_counts_and_issue_map() -> None:
    assert cliModule._tierCounts(PLAN) == {"A": 4, "B": 5, "C": 3}
    assert cliModule._issueMap([("K1", ("a", "b"))]) == {"K1": ("a", "b")}


def test_measure_text_handles_absent_measure() -> None:
    assert cliModule._measureText(None) == "（无结构化度量）"
    assert "RECEIPT_DATE" in cliModule._measureText(PLAN[0].numerator)


def test_schema_gap_hint_detects_missing_migration_only() -> None:
    missing = ProgrammingError(
        "SELECT 1", {}, RuntimeError('column "grain_keys" does not exist')
    )
    hint = cliModule._schemaGapHint(missing)
    assert hint is not None and "alembic upgrade head" in hint
    other = ProgrammingError("SELECT 1", {}, RuntimeError("syntax error"))
    assert cliModule._schemaGapHint(other) is None


def test_write_checklist_noop_and_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cliModule, "REPO_ROOT", tmp_path)
    cliModule._writeChecklist(None, "ignored")
    cliModule._writeChecklist("review/out.md", "# 清单")
    written = tmp_path / "review" / "out.md"
    assert written.read_text(encoding="utf-8") == "# 清单"
    absolute = tmp_path / "abs.md"
    cliModule._writeChecklist(str(absolute), "x")
    assert absolute.exists()


def test_backup_path_uses_today_and_table_name() -> None:
    path = cliModule._backupPath("ontology_metric")
    assert path.parent == Path("/tmp")
    assert path.name.startswith("ontology_metric_") and path.suffix == ".sql"


def _fakeDump(status: int, stderr: bytes = b"") -> object:
    return SimpleNamespace(returncode=status, stderr=stderr)


def test_pg_dump_accepts_missing_table_and_rejects_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "a.sql"

    def _runSkip(command: list[str], stdout: object, stderr: int) -> object:
        stdout.write(b"")  # type: ignore[attr-defined]
        return _fakeDump(1, b'pg_dump: error: no matching tables were found')

    monkeypatch.setattr(cliModule.subprocess, "run", _runSkip)
    cliModule._pgDump("postgresql+asyncpg://u:p@h:5432/qa_metadata", "t", out)
    assert not out.exists()

    monkeypatch.setattr(
        cliModule.subprocess,
        "run",
        lambda command, stdout, stderr: _fakeDump(1, b"boom"),
    )
    with pytest.raises(SystemExit, match="备份失败"):
        cliModule._pgDump("postgresql+asyncpg://u:p@h:5432/qa_metadata", "t", out)


def test_pg_dump_rejects_non_postgres_dsn(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="PostgreSQL"):
        cliModule._pgDump("sqlite:///x.db", "t", tmp_path / "a.sql")


def test_ensure_prod_backup_requires_all_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cliModule, "_BACKUP_DIR", tmp_path)

    def _write(command: list[str], table: str, path: Path) -> None:
        path.write_bytes(b"-- dump")

    monkeypatch.setattr(cliModule, "_pgDump", _write)
    paths = cliModule._ensureProdBackup("postgresql://u:p@h/qa_metadata")
    assert len(paths) == len(cliModule._BACKUP_TABLES)

    for path in paths:
        path.unlink()
    monkeypatch.setattr(cliModule, "_pgDump", lambda *a, **k: None)
    with pytest.raises(SystemExit, match="备份不完整"):
        cliModule._ensureProdBackup("postgresql://u:p@h/qa_metadata")


def test_print_banner_and_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dump = tmp_path / "d.sql"
    dump.write_bytes(b"12345")
    cliModule._printBanner("qa_metadata", (dump,))
    summary = cliModule.ApplySummary(
        created=("A",),
        updated=("B",),
        degraded=("C",),
        skipped=("D",),
        unboundDataSource=("E",),
        unresolvedClasses=("F",),
    )
    cliModule._printSummary(summary, "qa_metadata")
    out = capsys.readouterr().out
    assert "正在写正式库 qa_metadata" in out
    assert "备份：/tmp" not in out  # 用的是 tmp_path 而不是硬编码 /tmp
    assert "created=1 updated=1 degraded=1 skipped=1" in out
    assert "未绑定数据源的 KPI（1 条" in out


def test_main_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cliModule.main(["--help"])
    assert excinfo.value.code == 0


def test_main_without_database_url_exits() -> None:
    with pytest.raises(SystemExit, match="--database-url"):
        cliModule.main([])
