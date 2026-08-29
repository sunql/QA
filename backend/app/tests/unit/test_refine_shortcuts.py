"""REFINE 捷径：纯代码 SQL 改写单元测试（Phase D）。

_applyRefineDirect 在 REFINE 意图下优先改写上一轮 SQL（行数/排序/简单等值筛选），
不改写时返回 None 让流水线退回 LLM 两阶段。排序列/筛选列必须来自上一轮查询计划，
值严格校验（拒绝引号/分号/注释注入），从而不引入新的 SQL 注入面。
"""

from __future__ import annotations

from app.domain.query_plan import QueryPlan
from app.services.nl2sql_service import Nl2SqlService, _REFINE_MAX_LIMIT, _normalizeDate


def _plan(*props: str) -> QueryPlan:
    return QueryPlan(target="t", selectedProperties=tuple(props))


def _direct(sql: str, plan: QueryPlan | None, question: str) -> str | None:
    return Nl2SqlService().applyRefineDirect(sql, plan, question)


class TestRefineDirect:
    # ---------- 行数 ----------

    def test_modifies_fetch_first(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T ORDER BY NAME ASC FETCH FIRST 10 ROWS ONLY"
        out = _direct(sql, _plan("NAME"), "只要前 3 条")
        assert out is not None
        assert "FETCH FIRST 3 ROWS ONLY" in out

    def test_modifies_limit(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T LIMIT 10"
        out = _direct(sql, _plan(), "只看前 5 名")
        assert out is not None
        assert out == "SELECT NAME FROM ZJTH.T LIMIT 5"

    def test_limit_without_existing_limit_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan(), "只要前 3 条") is None

    # ---------- 排序 ----------

    def test_appends_order_by_before_fetch_first(self) -> None:
        sql = "SELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT GROUP BY NAME FETCH FIRST 10 ROWS ONLY"
        out = _direct(sql, _plan("NAME", "QTY"), "按 QTY 升序排列")
        assert out is not None
        assert "GROUP BY NAME ORDER BY QTY ASC FETCH FIRST 10 ROWS ONLY" in out

    def test_appends_order_by_at_end(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME"), "按 NAME 降序")
        assert out is not None
        assert out == "SELECT NAME FROM ZJTH.T ORDER BY NAME DESC"

    def test_replaces_existing_order_by(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T ORDER BY NAME ASC"
        out = _direct(sql, _plan("NAME"), "按 NAME 降序")
        assert out is not None
        assert out.endswith("ORDER BY NAME DESC")

    def test_sort_unknown_column_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME"), "按 GHOST 升序") is None

    def test_sort_without_plan_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, None, "按 NAME 升序") is None

    # ---------- 简单等值筛选 ----------

    def test_appends_and_to_existing_where(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T WHERE QTY > 1 ORDER BY NAME ASC"
        out = _direct(sql, _plan("NAME"), "只看 NAME 为 A 的")
        assert out is not None
        assert "WHERE QTY > 1 AND NAME = 'A' ORDER BY NAME ASC" in out

    def test_adds_where_before_order_by_when_missing(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T ORDER BY NAME ASC"
        out = _direct(sql, _plan("NAME"), "只看 NAME 等于 B 的")
        assert out is not None
        assert "WHERE NAME = 'B' ORDER BY NAME ASC" in out

    def test_adds_where_before_group_by(self) -> None:
        sql = "SELECT NAME, SUM(QTY) FROM ZJTH.T GROUP BY NAME"
        out = _direct(sql, _plan("NAME"), "只看 NAME 为 A 的")
        assert out is not None
        assert "WHERE NAME = 'A' GROUP BY NAME" in out

    def test_numeric_value_not_quoted(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "QTY"), "只看 QTY 为 100 的")
        assert out is not None
        assert "WHERE QTY = 100" in out

    def test_quote_injection_value_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T WHERE QTY > 1"
        out = _direct(sql, _plan("NAME"), "只看 NAME 为 ' OR 1=1 -- 的")
        assert out is None

    # ---------- 无法改写 ----------

    def test_unknown_refine_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME"), "重新换成其他方案") is None

    def test_empty_inputs_return_none(self) -> None:
        assert _direct("", _plan("NAME"), "按 NAME 升序") is None
        assert _direct("SELECT 1", _plan("NAME"), "") is None

    # ---------- 安全加固（security-review 评审项） ----------

    def test_backslash_in_filter_value_rejected(self) -> None:
        # MEDIUM-1：反斜杠可作为部分方言的转义符，拒绝以免闭合字符串字面量
        sql = "SELECT NAME FROM ZJTH.T WHERE QTY > 1"
        assert _direct(sql, _plan("NAME"), r"只看 NAME 为 test\ 的") is None

    def test_unsafe_identifier_in_plan_not_used(self) -> None:
        # MEDIUM-2：非安全标识符（含分号/空格）的列不得进入 SQL 改写
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME;DROP"), "按 NAME;DROP 升序") is None

    def test_filter_column_case_insensitive(self) -> None:
        # MEDIUM-3：用户写小写列名也能命中计划中的真实列名
        sql = "SELECT NAME FROM ZJTH.T ORDER BY NAME ASC"
        out = _direct(sql, _plan("NAME"), "只看 name 为 A 的")
        assert out is not None
        assert "WHERE NAME = 'A'" in out

    def test_extract_limit_is_clamped(self) -> None:
        # LOW-3：超大行数钳制到上限，避免拖慢查询规划
        sql = "SELECT NAME FROM ZJTH.T LIMIT 10"
        out = _direct(sql, _plan(), "只看前 999999999 条")
        assert out is not None
        assert out == f"SELECT NAME FROM ZJTH.T LIMIT {_REFINE_MAX_LIMIT}"

    # ---- 范围感知行数限制（REFINE 已知缺口） ----

    def test_refine_keeps_existing_row_limit_when_filter_added(self) -> None:
        """24（缺口固化）：上一轮 SQL 带 FETCH FIRST 100（无范围兜底），追问加 WHERE 后行数限制保留。

        已知缺口（见 changes/feat-scope-aware-row-limit/summary.md §4.1）：
        REFINE 捷径无法判断新问题的"范围"，因此追加筛选条件时保留上一轮行数限制；
        带"时间范围"的追问因 _REFINE_CMP_RE 要求"列 运算符 值"结构而命中不了捷径，
        自然退回两阶段拿到正确行为。锁定该行为防止无意变更。
        """
        sql = "SELECT NAME FROM ZJTH.T WHERE CATEGORY='A' FETCH FIRST 100 ROWS ONLY"
        # 列名用 ASCII（_REFINE_CMP_RE 的 \w+ 不匹配 CJK），运算符用"为"（中文算子）
        out = _direct(sql, _plan("NAME", "CATEGORY", "STATE"), "只看 STATE 为 CLOSED")
        assert out is not None
        # 行数限制保留（缺口行为），并追加了新 WHERE 条件
        assert "FETCH FIRST 100 ROWS ONLY" in out
        assert "STATE = 'CLOSED'" in out
        assert "CATEGORY='A'" in out


class TestRefineExtension35:
    """3-5 REFINE 捷径扩展：中文列名(C9)、聚合别名排序(C8)、范围/排除/日期筛选。

    白名单放行中文标识符（CJK 无法闭合 SQL 字符串/注释），但分隔符/引号仍拒绝；
    聚合别名仅参与排序列匹配（WHERE 中引用聚合非法）；日期范围严格校验后转 BETWEEN。
    """

    # ---------- C9：中文列名 ----------

    def test_sort_with_chinese_column(self) -> None:
        sql = "SELECT 收货数量 FROM ZJTH.T"
        out = _direct(sql, _plan("收货数量"), "按 收货数量 降序")
        assert out is not None
        assert out == "SELECT 收货数量 FROM ZJTH.T ORDER BY 收货数量 DESC"

    def test_filter_with_chinese_column(self) -> None:
        sql = "SELECT 名称 FROM ZJTH.T"
        out = _direct(sql, _plan("名称", "状态"), "只看 状态 为 已关闭 的")
        assert out is not None
        assert "WHERE 状态 = '已关闭'" in out

    def test_chinese_identifier_with_injection_chars_rejected(self) -> None:
        # MEDIUM-2 收紧为"允许中文"，但含分隔符/引号的标识符仍不得拼入 SQL
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("金额;DROP"), "按 金额;DROP 降序") is None

    def test_sort_bare_desc_without_prefix(self) -> None:
        # 无"按"前缀的裸排序也命中（标识符边界防子串误匹配）
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME"), "NAME 降序")
        assert out is not None
        assert out == "SELECT NAME FROM ZJTH.T ORDER BY NAME DESC"

    def test_sort_bare_asc_without_prefix(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME"), "NAME 升序")
        assert out is not None
        assert out.endswith("ORDER BY NAME ASC")

    def test_sort_bare_sort_without_prefix(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME"), "NAME 排序")
        assert out is not None
        assert out.endswith("ORDER BY NAME ASC")

    def test_sort_boundary_prevents_substring_match(self) -> None:
        # 计划列 QTY 不得命中别名 TOTAL_QTY 的子串；裸 "TOTAL_QTY 降序" 命中别名
        sql = "SELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "QTY"), "TOTAL_QTY 降序")
        assert out is not None
        assert out.endswith("ORDER BY TOTAL_QTY DESC")

    def test_sort_with_sort_keyword(self) -> None:
        # "按 X 排序" 默认升序（与"按 X 升序"同向）
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME"), "按 NAME 排序")
        assert out is not None
        assert out.endswith("ORDER BY NAME ASC")

    def test_normalize_date_malformed_returns_none(self) -> None:
        # 防御：完全不匹配日期格式的输入返回 None（_normalizeDate 作为独立工具的守卫）
        assert _normalizeDate("not-a-date") is None

    # ---------- C8：聚合别名排序 ----------

    def test_sort_by_chinese_aggregation_alias(self) -> None:
        sql = "SELECT 供应商, SUM(金额) AS 总额 FROM ZJTH.T GROUP BY 供应商 ORDER BY 总额 DESC"
        out = _direct(sql, _plan("供应商", "金额"), "按 总额 降序")
        assert out is not None
        assert out.endswith("ORDER BY 总额 DESC")

    def test_sort_by_english_aggregation_alias(self) -> None:
        sql = "SELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.T GROUP BY NAME"
        out = _direct(sql, _plan("NAME", "QTY"), "按 TOTAL_QTY 升序")
        assert out is not None
        assert "ORDER BY TOTAL_QTY ASC" in out

    def test_aggregation_alias_not_used_for_filter(self) -> None:
        # 聚合别名仅用于排序列；作为 WHERE 列（聚合在 WHERE 中非法）应退回 LLM
        sql = "SELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.T"
        assert _direct(sql, _plan("NAME", "QTY"), "只看 TOTAL_QTY 为 100 的") is None

    def test_inner_subquery_alias_ignored(self) -> None:
        # 只匹配顶层 SELECT 的别名，子查询内的 AS 不参与排序列匹配
        sql = "SELECT NAME FROM ZJTH.T WHERE ID IN (SELECT SUM(X) AS GHOST FROM ZJTH.U)"
        out = _direct(sql, _plan("NAME", "ID"), "按 GHOST 降序")
        assert out is None

    # ---------- 范围比较 / 排除 ----------

    def test_filter_greater_than(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "QTY"), "只看 QTY 大于 100 的")
        assert out is not None
        assert "WHERE QTY > 100" in out

    def test_filter_greater_equal(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "QTY"), "筛选 QTY 大于等于 100 的")
        assert out is not None
        assert "WHERE QTY >= 100" in out

    def test_filter_less_than(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "QTY"), "只看 QTY 小于 10 的")
        assert out is not None
        assert "WHERE QTY < 10" in out

    def test_filter_not_equal(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "状态"), "只看 状态 不等于 已关闭 的")
        assert out is not None
        assert "WHERE 状态 <> '已关闭'" in out

    def test_filter_exclude_keyword(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "状态"), "排除 状态 为 已关闭 的")
        assert out is not None
        assert "WHERE 状态 <> '已关闭'" in out

    def test_exclude_unknown_column_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME"), "排除 GHOST 为 A 的") is None

    def test_exclude_quote_injection_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME", "状态"), "排除 状态 为 ' OR 1=1 -- 的") is None

    def test_filter_empty_value_returns_none(self) -> None:
        # 值为空（仅"的"被剥离后）-> _quoteFilterValue 拒绝，整体退回 LLM
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME", "状态"), "只看 状态 为 的") is None

    def test_cmp_unknown_column_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME"), "只看 GHOST 大于 5 的") is None

    # ---------- 日期范围 ----------

    def test_filter_date_range(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "下单日期"), "筛选 下单日期 在 2024-07-01 到 2024-07-31 之间")
        assert out is not None
        assert "WHERE 下单日期 BETWEEN '2024-07-01' AND '2024-07-31'" in out

    def test_filter_date_range_slashes_normalized(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "下单日期"), "筛选 下单日期 在 2024/7/1 至 2024/7/31 之间")
        assert out is not None
        assert "WHERE 下单日期 BETWEEN '2024-07-01' AND '2024-07-31'" in out

    def test_filter_date_range_reversed_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME", "下单日期"), "筛选 下单日期 在 2024-08-01 到 2024-07-31 之间") is None

    def test_filter_invalid_date_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME", "下单日期"), "筛选 下单日期 在 2024-13-01 到 2024-07-31 之间") is None

    def test_filter_invalid_day_returns_none(self) -> None:
        # 月合法但日越界（32 号）-> _normalizeDate 拒绝
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME", "下单日期"), "筛选 下单日期 在 2024-07-32 到 2024-08-31 之间") is None

    def test_filter_date_range_unknown_column_returns_none(self) -> None:
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME"), "筛选 下单日期 在 2024-07-01 到 2024-07-31 之间") is None

    def test_filter_invalid_calendar_date_returns_none(self) -> None:
        # 2 月没有 31 号 -> _normalizeDate 用真实日历校验拒绝（MEDIUM-3 修复）
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME", "下单日期"), "筛选 下单日期 在 2024-02-31 到 2024-03-01 之间") is None

    def test_filter_non_leap_feb_29_returns_none(self) -> None:
        # 2023 非闰年，2 月 29 号不存在
        sql = "SELECT NAME FROM ZJTH.T"
        assert _direct(sql, _plan("NAME", "下单日期"), "筛选 下单日期 在 2023-02-29 到 2023-03-01 之间") is None

    def test_filter_leap_feb_29_accepted(self) -> None:
        # 2024 闰年，2 月 29 号合法
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "下单日期"), "筛选 下单日期 在 2024-02-29 到 2024-03-01 之间")
        assert out is not None
        assert "WHERE 下单日期 BETWEEN '2024-02-29' AND '2024-03-01'" in out

    # ---------- 别名提取加固（MEDIUM-1 / MEDIUM-2） ----------

    def test_cast_type_not_extracted_as_alias(self) -> None:
        # CAST(金额 AS INT) 内的 AS 在括号内，不得把 INT 当成可排序别名
        sql = "SELECT CAST(金额 AS INT), 名称 FROM ZJTH.T"
        assert _direct(sql, _plan("名称", "金额"), "按 INT 降序") is None

    def test_from_in_string_literal_not_truncating(self) -> None:
        # 字符串字面量里的 FROM 不得截断 SELECT 列表
        sql = "SELECT 'FROM' AS 标签, 名称 AS 名称 FROM ZJTH.T"
        out = _direct(sql, _plan("名称"), "按 标签 降序")
        assert out is not None
        assert out.endswith("ORDER BY 标签 DESC")

    def test_scalar_subquery_alias_captured(self) -> None:
        # 标量子查询的 AS 别名在顶层（括号外），应可排序
        sql = "SELECT (SELECT MAX(x) FROM inner) AS 内部, 名称 FROM outer"
        out = _direct(sql, _plan("名称"), "按 内部 降序")
        assert out is not None
        assert out.endswith("ORDER BY 内部 DESC")

    def test_from_inside_identifier_not_truncating(self) -> None:
        # FROM 必须是独立词，不得匹配 "FROMAGE" 这类子串
        sql = "SELECT FROMAGE AS 奶酪 FROM ZJTH.T"
        out = _direct(sql, _plan("FROMAGE"), "按 奶酪 降序")
        assert out is not None
        assert out.endswith("ORDER BY 奶酪 DESC")

    def test_alias_after_line_comment(self) -> None:
        # 行注释不截断扫描：FROM 在注释后仍能定位
        sql = "SELECT NAME -- 旧列\nAS 名称 FROM ZJTH.T"
        out = _direct(sql, _plan("NAME"), "按 名称 降序")
        assert out is not None
        assert out.endswith("ORDER BY 名称 DESC")

    def test_alias_after_block_comment(self) -> None:
        sql = "SELECT NAME /* 备注 */ AS 名称 FROM ZJTH.T"
        out = _direct(sql, _plan("NAME"), "按 名称 降序")
        assert out is not None
        assert out.endswith("ORDER BY 名称 DESC")

    def test_alias_with_escaped_quote_in_string(self) -> None:
        # SQL 转义 '' 不提前闭合字符串，别名仍可提取
        sql = "SELECT 'it''s' AS 标签 FROM ZJTH.T"
        out = _direct(sql, _plan(), "按 标签 降序")
        assert out is not None
        assert out.endswith("ORDER BY 标签 DESC")

    # ---------- 值包含注释符仍安全（LOW-1 文档化） ----------

    def test_filter_value_with_block_comment_chars_quoted_safely(self) -> None:
        # 值含 /* ：字符串字面量内不会起注释作用，应被安全引号
        sql = "SELECT NAME FROM ZJTH.T"
        out = _direct(sql, _plan("NAME", "状态"), "只看 状态 为 a/*b 的")
        assert out is not None
        assert "WHERE 状态 = 'a/*b'" in out
