"""KPI 指标工具链的目标库守卫 + 验证脚本的输出语义。

**为什么守卫要有单测**：守卫是「绝不误写正式库」的唯一一道程序性防线。
它一旦写错（正则太宽 / 太窄），后果是写正式库或挡住合法目标 —— 都不可接受。

**为什么输出语义要有单测**：一次「9 步全 SKIP」的运行若退出码是 0，在任何闸门里
都会被读成「验证通过」（T16-13）—— 这是本脚本最坏的失败模式。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.kpi_metric_db_guard import (
    ALLOWED_DB_PATTERN,
    assertSafeTarget,
    databaseNameOf,
    maskDsn,
)
from scripts.kpi_metric_realdata_lib import (
    FAILURES,
    SKIPS,
    VERDICT_MISMATCH,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    check,
    expectedVerdictFor,
    guardTarget,
    lastSkipReason,
    printSummary,
    resolveDatabaseUrl,
)

# ── 库名解析 ─────────────────────────────────────────────────────────


def test_database_name_of_plain_dsn() -> None:
    assert (
        databaseNameOf("postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test")
        == "qa_metadata_test"
    )


def test_database_name_strips_query_and_fragment() -> None:
    assert (
        databaseNameOf(
            "postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test?ssl=require#x"
        )
        == "qa_metadata_test"
    )


def test_database_name_of_dsn_without_path() -> None:
    assert databaseNameOf("postgresql+asyncpg://u:p@localhost:5434") == ""


def test_database_name_of_non_dsn() -> None:
    assert databaseNameOf("not-a-dsn") == ""


# ── 白名单 ───────────────────────────────────────────────────────────


def test_pattern_allows_dev_and_test_only() -> None:
    assert ALLOWED_DB_PATTERN.match("qa_metadata")
    assert ALLOWED_DB_PATTERN.match("qa_metadata_test")
    assert ALLOWED_DB_PATTERN.match("qa_metadata_test_solo")
    assert ALLOWED_DB_PATTERN.match("qa_metadata_test_t16")
    assert not ALLOWED_DB_PATTERN.match("qa_metadata_prod")
    assert not ALLOWED_DB_PATTERN.match("postgres")
    assert not ALLOWED_DB_PATTERN.match("qa_metadata_backup")
    # 前缀相似的近似名不受信：`qa_metadata_test*` 必须是完整名或以 `_` 续名
    assert not ALLOWED_DB_PATTERN.match("qa_metadata_testing")


def test_assert_safe_target_accepts_test_database_without_opt_in() -> None:
    assert (
        assertSafeTarget("postgresql+asyncpg://u:p@localhost:5434/qa_metadata_test")
        == "qa_metadata_test"
    )


def test_assert_safe_target_rejects_production_database_without_opt_in() -> None:
    """⚠️ **正式库名就是 `qa_metadata`**（与 dev 同名）——不是 ``qa_metadata_prod``。

    库名白名单**无法**区分二者 ⇒ 只能靠**显式 opt-in**（``allowProdWrite=True``）
    当第二道闸。本用例必须用**真库名**，用编出来的名字等于零证明力。
    """
    with pytest.raises(SystemExit) as exc:
        assertSafeTarget("postgresql+asyncpg://u:p@localhost:5433/qa_metadata")
    assert "qa_metadata" in str(exc.value)


def test_assert_safe_target_accepts_production_database_with_opt_in() -> None:
    assert (
        assertSafeTarget(
            "postgresql+asyncpg://u:p@localhost:5433/qa_metadata",
            allowProdWrite=True,
        )
        == "qa_metadata"
    )


def test_assert_safe_target_rejects_unrelated_database() -> None:
    with pytest.raises(SystemExit):
        assertSafeTarget("postgresql+asyncpg://u:p@localhost:5432/postgres")


def test_guard_target_passes_opt_in_through() -> None:
    with pytest.raises(SystemExit):
        guardTarget("postgresql+asyncpg://u:p@localhost:5433/qa_metadata")
    assert (
        guardTarget(
            "postgresql+asyncpg://u:p@localhost:5433/qa_metadata",
            allowProdWrite=True,
        )
        == "qa_metadata"
    )


# ── 口令打码 ─────────────────────────────────────────────────────────


def test_mask_dsn_hides_password() -> None:
    masked = maskDsn("postgresql+asyncpg://qa_user:sup3rsecret@localhost:5434/db")
    assert "sup3rsecret" not in masked
    assert "qa_user" in masked
    assert masked.endswith("@localhost:5434/db")


def test_mask_dsn_without_credentials() -> None:
    assert maskDsn("postgresql+asyncpg://localhost:5434/db") == "***"


# ── DSN 解析顺序 ─────────────────────────────────────────────────────


def test_resolve_database_url_prefers_explicit_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KPI_METRIC_REALDATA_DATABASE_URL",
        "postgresql+asyncpg://u:p@h:5434/from_env",
    )
    dsn, source = resolveDatabaseUrl("postgresql+asyncpg://u:p@h:5434/explicit")
    assert dsn.endswith("/explicit")
    assert source == "--database-url"


def test_resolve_database_url_falls_back_to_dedicated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KPI_METRIC_REALDATA_DATABASE_URL",
        "postgresql+asyncpg://u:p@h:5434/explicit",
    )
    dsn, source = resolveDatabaseUrl()
    assert dsn.endswith("/explicit")
    assert source == "KPI_METRIC_REALDATA_DATABASE_URL"


def test_resolve_database_url_ignores_test_and_database_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TEST_DATABASE_URL`` / ``DATABASE_URL`` **不参与**解析（T16-03）。

    本仓 dev 工作流普遍导出 ``TEST_DATABASE_URL``；若作为回退，S1–S9 会打到
    ``qa_metadata_test``（那里没有那 12 条 KPI）⇒ 验证对象被偷换。
    """
    monkeypatch.delenv("KPI_METRIC_REALDATA_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://u:p@h:5434/from_test"
    )
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://u:p@h:5434/from_database"
    )
    with pytest.raises(SystemExit):
        resolveDatabaseUrl()


def test_resolve_database_url_requires_explicit_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "KPI_METRIC_REALDATA_DATABASE_URL",
        "TEST_DATABASE_URL",
        "DATABASE_URL",
        "PGPASSWORD",
        "POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        resolveDatabaseUrl()


# ── PASS / FAIL / SKIP 语义 ──────────────────────────────────────────


def test_print_summary_never_claims_all_passed_when_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FAILURES.clear()
    SKIPS.clear()
    SKIPS.append(("S9 回填落库", "库种子不满足"))
    exitCode = printSummary()
    out = capsys.readouterr().out
    assert "all steps passed" not in out
    assert "NOTE" in out
    assert "S9 回填落库" in out
    # SKIP 不进失败清单，但**影响退出码**：无 FAIL 但有 SKIP ⇒ 2（三态）
    assert exitCode == 2
    # 机器可读结论行（T16-15）：缺这行 ⇒ stdout 被截断
    assert "RESULT: SKIP" in out
    assert "steps=9" in out


def test_print_summary_all_passed_only_without_failures_and_skips(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FAILURES.clear()
    SKIPS.clear()
    exitCode = printSummary()
    out = capsys.readouterr().out
    assert "OK  all steps passed" in out
    assert "RESULT: PASS steps=9" in out
    assert exitCode == 0


def test_print_summary_failure_sets_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FAILURES.clear()
    SKIPS.clear()
    FAILURES.append("S1.1 迁移版本: 实际 '0135'")
    exitCode = printSummary()
    out = capsys.readouterr().out
    assert exitCode == 1
    assert "FAILED" in out
    assert "all steps passed" not in out
    assert "RESULT: FAIL steps=9" in out


def test_print_summary_counts_steps_not_assertions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """同一 S2 下多条断言失败 ⇒ 只算 **1 个失败步骤**（T16-15）。"""
    FAILURES.clear()
    SKIPS.clear()
    FAILURES.extend(["S2.1 a: x", "S2.2 b: y", "S2.3 c: z"])
    exitCode = printSummary()
    out = capsys.readouterr().out
    assert exitCode == 1
    assert "failed=1" in out


def test_print_summary_counts_unlabelled_failure_as_one_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """建连就崩（``MAIN 执行异常``，无步骤前缀）不得被算成 0 个失败步骤。"""
    FAILURES.clear()
    SKIPS.clear()
    FAILURES.append("MAIN 执行异常: InvalidPasswordError(...)")
    exitCode = printSummary()
    out = capsys.readouterr().out
    assert exitCode == 1
    assert "failed=1" in out
    assert "passed=8" in out


def test_check_records_failure_without_raising() -> None:
    FAILURES.clear()
    SKIPS.clear()
    assert check("S0 自检", False, "故意失败") is False
    assert len(FAILURES) == 1
    assert check("S0 自检", True, "正常") is True
    assert len(FAILURES) == 1


def test_last_skip_reason_returns_most_recent_prefix_match() -> None:
    SKIPS.clear()
    SKIPS.append(("S3 三层对账", "原因 A"))
    SKIPS.append(("S7.2 findByAnyKeyword", "原因 B"))
    assert lastSkipReason("S3") == "原因 A"
    assert lastSkipReason("S7") == "原因 B"
    assert lastSkipReason("S9") is None


# ── S3 期望 verdict（FIX-2：无偏差分支必须被真实执行过） ──────────────
#
# 真实 12 条 PLAN **全部**有 deviations ⇒ S3 里「无偏差」的两条分支实跑
# 永不执行。这里构造 ``deviations=()`` 的 plan 输入直接跑这两条路径。


def test_expected_verdict_without_deviation_tier_a_is_pass() -> None:
    plan = SimpleNamespace(tier="A", deviations=())
    assert expectedVerdictFor(plan.tier, plan.deviations) == VERDICT_PASS


def test_expected_verdict_without_deviation_tier_b_is_unknown() -> None:
    plan = SimpleNamespace(tier="B", deviations=())
    assert expectedVerdictFor(plan.tier, plan.deviations) == VERDICT_UNKNOWN


def test_expected_verdict_without_deviation_tier_c_is_unknown() -> None:
    plan = SimpleNamespace(tier="C", deviations=())
    assert expectedVerdictFor(plan.tier, plan.deviations) == VERDICT_UNKNOWN


def test_expected_verdict_with_deviation_is_mismatch_even_tier_a() -> None:
    plan = SimpleNamespace(tier="A", deviations=("governance↔物理列偏差",))
    assert expectedVerdictFor(plan.tier, plan.deviations) == VERDICT_MISMATCH


def test_expected_verdict_unknown_tier_without_deviation_is_unknown() -> None:
    """PLAN 里查不到该 code（tier=None）⇒ 不得当成 pass。"""
    assert expectedVerdictFor(None, ()) == VERDICT_UNKNOWN


def test_verdict_literals_match_reconcile_module() -> None:
    """lib 保持零 app 依赖（入口 import 早于 DATABASE_URL 设置）⇒ 字面量
    必须由测试钉住，防止两处漂移。"""
    from app.services.kpi_metric_reconcile import (
        VERDICT_MISMATCH as RECONCILE_MISMATCH,
    )
    from app.services.kpi_metric_reconcile import (
        VERDICT_PASS as RECONCILE_PASS,
    )
    from app.services.kpi_metric_reconcile import (
        VERDICT_UNKNOWN as RECONCILE_UNKNOWN,
    )

    assert VERDICT_PASS == RECONCILE_PASS
    assert VERDICT_MISMATCH == RECONCILE_MISMATCH
    assert VERDICT_UNKNOWN == RECONCILE_UNKNOWN
