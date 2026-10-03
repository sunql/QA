"""Top-N 占比分母守卫（feat-nl2sql-share-denominator-guard）单元测试。

L1 形态拦截：findShareDenominatorIssues —— 判别器是 2026-10-02 真机报障的
两条 SQL 原文（RUN1_SQL 陷阱 / RUN2_SQL 正确），任何改动后两者断言都必须保持。
L3 结果不变量 + L2 歧义示警：checkShareInvariants —— 数学判据，Decimal 归一化。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.query_plan import Aggregation, QueryPlan
from app.services.nl2sql_semantic_guard import (
    ShareCheckResult,
    checkShareInvariants,
    findShareDenominatorIssues,
    shareAmbiguityWarning,
)

# ---------------------------------------------------------------------------
# fixtures：2026-10-02 真机同一问题两次生成的 SQL 原文（判别器，禁止改写）
# ---------------------------------------------------------------------------

RUN1_SQL = """
WITH item_qty AS (
    SELECT
        r.SUPPLIER_CODE AS SUPPLIER_CODE,
        r.SUPPLIER_NAME AS SUPPLIER_NAME,
        r.ITEM_CODE AS ITEM_CODE,
        SUM(r.RCV_QTY_PUU) AS ITEM_QTY
    FROM THBI.DWD_GOODS_RECEIPT_DTL r
    WHERE r.SUPPLIER_CODE IN ('B019','B125','B153')
      AND r.RCV_DATE >= DATE '2026-04-01'
      AND r.RCV_DATE <  DATE '2026-05-01'
    GROUP BY r.SUPPLIER_CODE, r.SUPPLIER_NAME, r.ITEM_CODE
),
ranked AS (
    SELECT
        SUPPLIER_CODE,
        SUPPLIER_NAME,
        ITEM_CODE,
        ITEM_QTY,
        ROW_NUMBER() OVER (PARTITION BY SUPPLIER_CODE ORDER BY ITEM_QTY DESC NULLS LAST) AS RNK
    FROM item_qty
)
SELECT
    SUPPLIER_CODE,
    SUPPLIER_NAME,
    SUM(ITEM_QTY) AS TOP3_QTY,
    SUM(SUM(ITEM_QTY)) OVER (PARTITION BY SUPPLIER_CODE) AS SUPPLIER_TOTAL_QTY,
    SUM(ITEM_QTY) / NULLIF(SUM(SUM(ITEM_QTY)) OVER (PARTITION BY SUPPLIER_CODE), 0) AS TOP3_SHARE
FROM ranked
WHERE RNK <= 3
GROUP BY SUPPLIER_CODE, SUPPLIER_NAME
ORDER BY SUPPLIER_CODE ASC
"""

RUN2_SQL = """
WITH item_qty AS (
    SELECT
        r.SUPPLIER_CODE AS SUPPLIER_CODE,
        r.ITEM_CODE AS ITEM_CODE,
        SUM(r.RCV_QTY_PUU) AS ITEM_QTY
    FROM THBI.DWD_GOODS_RECEIPT_DTL r
    WHERE r.SUPPLIER_CODE IN ('B019','B125','B153')
      AND r.RCV_DATE >= DATE '2026-04-01'
      AND r.RCV_DATE <  DATE '2026-05-01'
    GROUP BY r.SUPPLIER_CODE, r.ITEM_CODE
),
ranked AS (
    SELECT
        SUPPLIER_CODE,
        ITEM_CODE,
        ITEM_QTY,
        ROW_NUMBER() OVER (PARTITION BY SUPPLIER_CODE ORDER BY ITEM_QTY DESC NULLS LAST) AS RN
    FROM item_qty
),
sup_total AS (
    SELECT
        SUPPLIER_CODE,
        SUM(ITEM_QTY) AS TOTAL_QTY
    FROM item_qty
    GROUP BY SUPPLIER_CODE
)
SELECT
    r.SUPPLIER_CODE AS SUPPLIER_CODE,
    SUM(r.ITEM_QTY) AS TOP3_QTY,
    t.TOTAL_QTY AS TOTAL_QTY,
    SUM(r.ITEM_QTY) / NULLIF(t.TOTAL_QTY, 0) AS TOP3_SHARE
FROM ranked r
JOIN sup_total t ON t.SUPPLIER_CODE = r.SUPPLIER_CODE
WHERE r.RN <= 3
GROUP BY r.SUPPLIER_CODE, t.TOTAL_QTY
ORDER BY TOP3_SHARE DESC NULLS LAST
"""


def _sharePlan(*, perGroupLimit: int | None = 3, rowLimit: int | None = None) -> QueryPlan:
    """带占比聚合的 Top-N 计划（alias 与真机 SQL 输出列一致）。"""
    return QueryPlan(
        target="三家供应商Top3物料占比",
        partitionBy=("供应商编码",),
        perGroupLimit=perGroupLimit,
        rowLimit=rowLimit,
        aggregations=(
            Aggregation(
                function="SUM",
                property="收货数量",
                alias="TOP3_SHARE",
                formula="SUM(ITEM_QTY) / NULLIF(SUM(SUM(ITEM_QTY)) OVER (), 0)",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# L1：findShareDenominatorIssues
# ---------------------------------------------------------------------------


class TestFindShareDenominatorIssues:
    def test_run1_trap_is_flagged(self) -> None:
        """判别器：真机陷阱 SQL（WHERE 后窗口分母）必须被拦。"""
        issues = findShareDenominatorIssues(RUN1_SQL)
        assert issues, "陷阱 SQL 必须产生问题"

    def test_run2_sup_total_cte_passes(self) -> None:
        """判别器：真机正确 SQL（独立 CTE 分母 JOIN）必须放行。"""
        assert findShareDenominatorIssues(RUN2_SQL) == ()

    def test_none_and_empty_pass(self) -> None:
        assert findShareDenominatorIssues(None) == ()
        assert findShareDenominatorIssues("") == ()

    def test_alias_qualified_rank_filter_flagged(self) -> None:
        """变体：WHERE r.RN <= 3（带别名前缀）+ 同块窗口分母 → 拦。"""
        sql = """
        SELECT r.SUPPLIER_CODE,
               SUM(r.ITEM_QTY) / SUM(SUM(r.ITEM_QTY)) OVER (PARTITION BY r.SUPPLIER_CODE) AS share
        FROM ranked r
        WHERE r.RN <= 3
        GROUP BY r.SUPPLIER_CODE
        """
        assert findShareDenominatorIssues(sql)

    def test_lowercase_and_no_space_flagged(self) -> None:
        """变体：小写列名、无空格比较符 → 拦。"""
        sql = (
            "SELECT s, SUM(x)/SUM(SUM(x)) OVER (PARTITION BY s) AS share "
            "FROM ranked WHERE rn<=3 GROUP BY s"
        )
        assert findShareDenominatorIssues(sql)

    def test_inner_window_outer_filter_passes(self) -> None:
        """合法形态：窗口分母在**内层块**算好（此时行集未过滤），排名过滤在外层块。"""
        sql = """
        WITH ranked AS (
            SELECT s, i, qty,
                   ROW_NUMBER() OVER (PARTITION BY s ORDER BY qty DESC) AS rn,
                   qty / SUM(qty) OVER (PARTITION BY s) AS share
            FROM item_qty
        )
        SELECT s, share FROM ranked WHERE rn <= 3
        """
        assert findShareDenominatorIssues(sql) == ()

    def test_plain_ratio_without_window_passes(self) -> None:
        """合法形态：普通比率（分母无 OVER）+ 排名过滤同块 → 放行（RUN2 的外层形态）。"""
        sql = (
            "SELECT s, SUM(x) / NULLIF(t.total, 0) AS share "
            "FROM ranked r JOIN sup_total t ON t.s = r.s "
            "WHERE r.rn <= 3 GROUP BY s"
        )
        assert findShareDenominatorIssues(sql) == ()

    def test_row_number_only_passes(self) -> None:
        """合法形态：仅 ROW_NUMBER + 过滤（无占比比率）→ 放行。"""
        sql = "SELECT s, x FROM ranked WHERE rn <= 3"
        assert findShareDenominatorIssues(sql) == ()

    def test_rank_filter_inside_string_literal_passes(self) -> None:
        """字符串字面量里的 rn<=3 不算过滤（教训同 formulaHasSqlStructure）。"""
        sql = (
            "SELECT s, SUM(x)/SUM(SUM(x)) OVER (PARTITION BY s) AS share "
            "FROM t WHERE note = 'rn <= 3' GROUP BY s"
        )
        assert findShareDenominatorIssues(sql) == ()

    def test_subquery_in_where_not_treated_as_same_block(self) -> None:
        """WHERE 里嵌子查询的 rn 过滤属于**子块**，与外层窗口分母不同块 → 放行。"""
        sql = """
        SELECT s,
               SUM(x) / SUM(SUM(x)) OVER (PARTITION BY s) AS share
        FROM t
        WHERE s IN (SELECT s FROM ranked WHERE rn <= 3)
        GROUP BY s
        """
        assert findShareDenominatorIssues(sql) == ()

    def test_trap_inside_subquery_block_flagged(self) -> None:
        """陷阱完整落在子查询块内（分母窗口 + 同块 rn 过滤）→ 仍要拦。"""
        sql = """
        SELECT * FROM (
            SELECT s,
                   SUM(x) / SUM(SUM(x)) OVER (PARTITION BY s) AS share
            FROM ranked
            WHERE rn <= 3
            GROUP BY s
        ) t
        """
        assert findShareDenominatorIssues(sql)

    def test_window_without_rank_filter_passes(self) -> None:
        """窗口分母但无 Top-N 过滤（全量占比）→ 合法。"""
        sql = (
            "SELECT s, SUM(x)/SUM(SUM(x)) OVER (PARTITION BY s) AS share "
            "FROM t GROUP BY s"
        )
        assert findShareDenominatorIssues(sql) == ()

    def test_lte_variant_flagged(self) -> None:
        """变体：rn < 4（等价于 <=3）→ 拦。"""
        sql = (
            "SELECT s, SUM(x)/SUM(SUM(x)) OVER (PARTITION BY s) AS share "
            "FROM ranked WHERE rn < 4 GROUP BY s"
        )
        assert findShareDenominatorIssues(sql)


# ---------------------------------------------------------------------------
# L3 + L2：checkShareInvariants
# ---------------------------------------------------------------------------


class TestCheckShareInvariants:
    def test_no_plan_or_no_data_is_noop(self) -> None:
        assert checkShareInvariants(None, [{"TOP3_SHARE": 1.0}]) == ShareCheckResult()
        assert checkShareInvariants(_sharePlan(), []) == ShareCheckResult()
        assert checkShareInvariants(_sharePlan(), None) == ShareCheckResult()

    def test_plan_without_topn_is_noop(self) -> None:
        plan = QueryPlan(
            target="占比",
            aggregations=(Aggregation(
                function="SUM", property="收货数量", alias="TOP3_SHARE",
                formula="SUM(x) / SUM(SUM(x)) OVER ()",
            ),),
        )
        data = [{"TOP3_SHARE": 1.2}]
        assert checkShareInvariants(plan, data) == ShareCheckResult()

    def test_plan_without_share_alias_is_noop(self) -> None:
        plan = QueryPlan(
            target="数量",
            perGroupLimit=3,
            aggregations=(Aggregation(function="SUM", property="收货数量", alias="QTY"),),
        )
        assert checkShareInvariants(plan, [{"QTY": 5}]) == ShareCheckResult()

    def test_share_above_one_is_violation(self) -> None:
        """单行占比 > 100% 数学上不可能（Top-N 是总量的子集）→ 确定性判错。"""
        result = checkShareInvariants(_sharePlan(), [{"TOP3_SHARE": 1.2}, {"TOP3_SHARE": 0.5}])
        assert result.violations
        assert result.warning is None

    def test_share_exactly_one_is_warning_not_violation(self) -> None:
        """全部恒 100% 是陷阱签名但也可能是合法（组内明细 ≤ N）→ 歧义示警不判错。"""
        data = [{"TOP3_SHARE": 1.0}, {"TOP3_SHARE": 1.0}]
        result = checkShareInvariants(_sharePlan(), data)
        assert result.violations == ()
        assert result.warning is not None

    def test_partial_shares_pass_silently(self) -> None:
        data = [{"TOP3_SHARE": 0.6}, {"TOP3_SHARE": 0.3}]
        assert checkShareInvariants(_sharePlan(), data) == ShareCheckResult()

    def test_decimal_string_values_coerced(self) -> None:
        """数值可能是 str/Decimal（Decimal/float 边界教训）：'1.2' 必须按数值判。"""
        result = checkShareInvariants(_sharePlan(), [{"TOP3_SHARE": "1.2"}])
        assert result.violations

    def test_decimal_values_coerced(self) -> None:
        result = checkShareInvariants(
            _sharePlan(), [{"TOP3_SHARE": Decimal("1.000001")}]
        )
        assert result.violations

    def test_null_share_rows_skipped(self) -> None:
        data = [{"TOP3_SHARE": None}, {"TOP3_SHARE": 0.5}]
        assert checkShareInvariants(_sharePlan(), data) == ShareCheckResult()

    def test_non_numeric_share_rows_skipped(self) -> None:
        data = [{"TOP3_SHARE": "N/A"}, {"TOP3_SHARE": 0.5}]
        assert checkShareInvariants(_sharePlan(), data) == ShareCheckResult()

    def test_case_insensitive_alias_match(self) -> None:
        """DB 返回列名大小写不定：top3_share 与 plan alias TOP3_SHARE 要能对上。"""
        result = checkShareInvariants(_sharePlan(), [{"top3_share": 1.5}])
        assert result.violations

    def test_group_sum_violation_with_resolvable_group_col(self) -> None:
        """组列能对上行键时按组求和：单行 ≤1 但同组多行加起来 >1 → 判错。"""
        plan = QueryPlan(
            target="各组占比",
            groupBy=("组",),
            perGroupLimit=3,
            aggregations=(Aggregation(
                function="SUM", property="收货数量", alias="TOP3_SHARE",
                formula="SUM(x) / SUM(SUM(x)) OVER ()",
            ),),
        )
        data = [
            {"TOP3_SHARE": 0.7, "组": "A"},
            {"TOP3_SHARE": 0.7, "组": "A"},
        ]
        result = checkShareInvariants(plan, data)
        assert result.violations

    def test_rowlimit_also_triggers_check(self) -> None:
        plan = QueryPlan(
            target="占比",
            rowLimit=3,
            aggregations=(Aggregation(
                function="SUM", property="收货数量", alias="TOP3_SHARE",
                formula="SUM(x) / SUM(SUM(x)) OVER ()",
            ),),
        )
        result = checkShareInvariants(plan, [{"TOP3_SHARE": 1.3}])
        assert result.violations

    def test_all_ones_warning_text_mentions_denominator(self) -> None:
        result = checkShareInvariants(_sharePlan(), [{"TOP3_SHARE": 1}])
        assert result.warning is not None
        assert "分母" in result.warning


class TestShareAmbiguityWarning:
    def test_returns_warning_text_only(self) -> None:
        assert shareAmbiguityWarning(_sharePlan(), [{"TOP3_SHARE": 1.0}]) is not None
        assert shareAmbiguityWarning(_sharePlan(), [{"TOP3_SHARE": 0.5}]) is None

    @pytest.mark.parametrize("plan", [None])
    def test_none_plan_returns_none(self, plan: QueryPlan | None) -> None:
        assert shareAmbiguityWarning(plan, [{"TOP3_SHARE": 1.0}]) is None


# ---------------------------------------------------------------------------
# L3 接线：_runQueryWithRetry 执行后核验（violations → Nl2SqlError）
# ---------------------------------------------------------------------------


class TestRunQueryShareCheckWiring:
    """violations 在执行出口抛 Nl2SqlError（单步回退多步 / 多步步隔离沿用既有路径）。"""

    def _stubService(self, rows: list[dict]):
        from app.services.chat_usage import UsageMixin

        class _Adapter:
            async def execute_read_only(self, sql: str) -> list[dict]:
                return rows

        class _Stub(UsageMixin):
            def __init__(self) -> None:
                self._adapterProvider = lambda dsId, ds: _Adapter()

        return _Stub()

    def _outcome(self, plan: QueryPlan | None):
        from types import SimpleNamespace

        return SimpleNamespace(plan=plan, sql="SELECT 1", sqlConfig=None)

    def _pc(self):
        from types import SimpleNamespace

        return SimpleNamespace(ds=SimpleNamespace(id=1))

    def _dto(self):
        from types import SimpleNamespace

        return SimpleNamespace(datasourceId=1, sessionId="s1", question="q")

    async def test_violation_raises_nl2sql_error(self) -> None:
        from app.domain.exceptions import Nl2SqlError
        from app.services.messages_zh import MSG_NL2SQL_SHARE_INVARIANT_FAILED

        svc = self._stubService([{"TOP3_SHARE": 1.5}])
        with pytest.raises(Nl2SqlError) as excInfo:
            await svc._runQueryWithRetry(
                None, self._dto(), self._pc(), self._outcome(_sharePlan())
            )
        assert MSG_NL2SQL_SHARE_INVARIANT_FAILED in str(excInfo.value)

    async def test_warning_returns_data_normally(self) -> None:
        svc = self._stubService([{"TOP3_SHARE": 1.0}])
        data, sql, retryTokens = await svc._runQueryWithRetry(
            None, self._dto(), self._pc(), self._outcome(_sharePlan())
        )
        assert data == [{"TOP3_SHARE": 1.0}]
        assert retryTokens == (0, 0)

    async def test_non_share_query_unaffected(self) -> None:
        plan = QueryPlan(target="数量", perGroupLimit=3)
        svc = self._stubService([{"QTY": 5}])
        data, _, _ = await svc._runQueryWithRetry(
            None, self._dto(), self._pc(), self._outcome(plan)
        )
        assert data == [{"QTY": 5}]
