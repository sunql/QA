"""季度时间口径注入 prompt + Oracle 季度边界/分组写法（2026-10-31）。

背景：同一问句在 dev/prod 生成了字面量与 ADD_MONTHS(TRUNC(CURRENT_DATE,'MM'),-N)
两种日期形态；季度边界完全由 LLM 推算，且 Oracle 季度分组写法 `'YYYY-Q'`
输出 2026-4（不是 2026-Q4）。
"""

from __future__ import annotations

from datetime import date

from app.domain.enums import DataSourceType
from app.services.nl2sql_dialects import DialectRegistry
from app.services.nl2sql_prompts import _buildPlanSystemPrompt, _buildSystemPrompt
from app.services.time_scope_resolver import parseTimeScope, renderTimeScopeFact


def _oracleDialect():
    return DialectRegistry.default().resolve(DataSourceType.ORACLE)


# ============================================================================
# 时间口径事实注入
# ============================================================================


def test_plan_prompt_injects_time_scope_fact() -> None:
    scope = parseTimeScope("本季度采购额", today=date(2026, 10, 31))
    fact = renderTimeScopeFact(scope)
    assert fact

    prompt = _buildPlanSystemPrompt(
        schemaText="T",
        dialect=_oracleDialect(),
        schemaPrefix=None,
        timeScopeFact=fact,
    )

    assert fact.strip() in prompt
    assert "2026年第4季度" in prompt


def test_sql_prompt_injects_time_scope_fact() -> None:
    scope = parseTimeScope("上一季度采购额", today=date(2026, 10, 31))
    fact = renderTimeScopeFact(scope)
    assert fact

    prompt = _buildSystemPrompt(
        schemaText="T",
        dialect=_oracleDialect(),
        schemaPrefix=None,
        timeScopeFact=fact,
    )

    assert fact.strip() in prompt
    assert "2026-07-01" in prompt


def test_prompt_without_time_scope_adds_no_block() -> None:
    """零回归：无时间口径时不注入空段 / 不改变既有 prompt 形态。"""
    for fact in (None, ""):
        planPrompt = _buildPlanSystemPrompt(
            schemaText="T",
            dialect=_oracleDialect(),
            schemaPrefix=None,
            timeScopeFact=fact,
        )
        sqlPrompt = _buildSystemPrompt(
            schemaText="T",
            dialect=_oracleDialect(),
            schemaPrefix=None,
            timeScopeFact=fact,
        )
        # 断言真实存在的注入标记（曾误写为代码里不存在的字符串 ⇒ 空断言假绿）
        assert "服务端按日历边界计算" not in planPrompt
        assert "服务端按日历边界计算" not in sqlPrompt
        assert "以下时间口径由服务端解析" not in planPrompt
        assert "以下时间口径由服务端解析" not in sqlPrompt


# ============================================================================
# Oracle 季度写法
# ============================================================================


def test_oracle_quarter_grouping_uses_quoted_q() -> None:
    """`'YYYY-Q'` 输出 2026-4；必须用 `'YYYY-"Q"Q'` 输出 2026-Q4。"""
    dialect = _oracleDialect()
    assert 'YYYY-"Q"Q' in dialect.timeBucketRule
    assert "'YYYY-Q'" not in dialect.timeBucketRule


def test_oracle_quarter_range_template_present() -> None:
    """季度区间必须给半开区间模板，避免 LLM 用 CURRENT_DATE 自己推算边界。"""
    dialect = _oracleDialect()
    rule = dialect.dateArithmeticRule
    assert "TRUNC(SYSDATE,'Q')" in rule
    assert "ADD_MONTHS(TRUNC(SYSDATE,'Q'),3)" in rule
    assert "含起不含止" in rule
