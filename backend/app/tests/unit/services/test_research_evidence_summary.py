"""研究步结果结构化证据摘要（Task 4）：从**取值**推断列类型的 JSON 安全统计摘要。

本测试钉住 controller 实施口径 D9–D15，逐条对应：
- D9：列类型无元数据，一律从取值推断；`columns` 参数可选、只按**名字**用（不要求 `type`）；
- D10：`question` 死参已删（反向断言签名里没有它）；
- D12：`stepResult.summary` 仍是 `str`，结构化摘要另起 `evidence` key（加法）；
- D13：`EvidenceSummary` 是 frozen `CamelModel`（snake 字段 + camel 线上别名），
  且 `empty`/`baseline`/`current`/`total` 可按 Python 字段名构造（Task 5 契约）；
- D14：含 `Decimal`/`date` 的摘要**真的能落 JSONB**（复用 `research_json`），
  并用反向断言证明这一步转换是**承重**的；
- D15：三个会真崩的 bug —— float 求和不炸、时间列含 NULL 排序不炸、`candidate_name`
  不被当成时间列。

JSONB 序列化用 SQLAlchemy 的 **JSONB bind processor** 直接触发 —— 与线上落
`state_payload` / `checkpoint.options` 是**同一条**序列化路径（默认 `json.dumps`），
无需真连库即可复现那记 `TypeError`。
"""

from __future__ import annotations

import inspect
import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from app.services.research_agent_ports import stepResult
from app.services.research_evidence_summary import (
    EvidenceSummary,
    _hasPeriodWord,
    _hasTimeToken,
    _hasTimeWord,
    _isIdentifierName,
    _isPeriodShaped,
    _isPureTimeName,
    inferColumnKinds,
    summarizeEvidence,
    trySummarizeEvidence,
)


def _jsonbSerializer():
    """SQLAlchemy `JSONB` 列的 bind serializer（每条落库 payload 都要过它）。"""
    return JSONB().bind_processor(postgresql.dialect())


# ---------------------------------------------------------------------------
# Happy path：数值列 + 时间列 → 总量/基线/当前/差值/增长率/TopN/样本
# ---------------------------------------------------------------------------


def test_happy_path_trend_over_time() -> None:
    rows = [
        {"year": 2024, "AMOUNT": Decimal("100")},
        {"year": 2025, "AMOUNT": Decimal("200")},
        {"year": 2026, "AMOUNT": Decimal("400")},
    ]

    summary = summarizeEvidence(rows)

    assert summary.total == "700"
    assert summary.baseline == "100"
    assert summary.current == "400"
    assert summary.delta == "300"
    assert summary.delta_ratio == "3"
    assert summary.empty is False
    # 时间列是整数 year：首尾值以字符串上轴（wire 契约）
    assert summary.time_range == {"start": "2024", "end": "2026"}
    assert [t["rank"] for t in summary.top_n] == [1, 2, 3]
    assert summary.top_n[0]["row"] == {"year": 2026, "AMOUNT": "400"}
    assert summary.sample_rows == [
        {"year": 2024, "AMOUNT": "100"},
        {"year": 2025, "AMOUNT": "200"},
        {"year": 2026, "AMOUNT": "400"},
    ]


def test_metric_column_excludes_the_time_column() -> None:
    """`year` 也被推断为数值（integer），但它是**时间轴**，不是被聚合的度量列。

    参考实现的 `numeric_cols[0]` 会取到 `year`（基线与它自己的测试断言 100 自相矛盾）。
    度量列必须排除时间列。
    """
    rows = [{"year": 2024, "amount": Decimal("100")}, {"year": 2025, "amount": Decimal("200")}]

    summary = summarizeEvidence(rows)

    assert summary.total == "300"
    assert summary.baseline == "100" and summary.current == "200"


def test_integer_axis_is_not_the_metric_when_a_real_timestamp_exists() -> None:
    """**最危险的组合**：整数轴（year）**加**一个真时间戳列（d）。

    `_timeColumn` 会正确地选 `d`，于是「第一个数值列」落到 `year` 上 —— `total` 变成
    `2024 + 2025 = 4049`、基线/当前变成**年份**，同比报表会拿年份比年份，看着还挺合理。
    度量列必须在**真时间戳存在**时避开带时间词的数值列。
    """
    rows = [
        {"year": 2024, "amount": Decimal("100"), "d": date(2024, 1, 1)},
        {"year": 2025, "amount": Decimal("200"), "d": date(2025, 1, 1)},
    ]

    summary = summarizeEvidence(rows)

    assert summary.total == "300"  # 不是 4049（2024 + 2025）
    assert summary.baseline == "100" and summary.current == "200"
    assert summary.delta_ratio == "1"
    assert summary.time_range == {"start": "2024-01-01", "end": "2025-01-01"}


@pytest.mark.parametrize("axis", ["year", "quarter", "month", "period", "week", "day"])
def test_any_time_worded_integer_axis_is_not_the_metric(axis: str) -> None:
    """同类缺陷对所有带时间词的整数轴都成立（不是 `year` 的特例）。"""
    rows = [
        {axis: 1, "amount": Decimal("100"), "d": date(2024, 1, 1)},
        {axis: 2, "amount": Decimal("200"), "d": date(2025, 1, 1)},
    ]

    summary = summarizeEvidence(rows)

    assert summary.total == "300"
    assert summary.baseline == "100" and summary.current == "200"


def test_top_n_ranks_by_measure_not_by_axis() -> None:
    """TopN 按同一度量列排名 —— 轴当度量时排序会一起错（本数据下下标序恰好反转）。"""
    rows = [
        {"year": 2024, "amount": Decimal("200"), "d": date(2024, 1, 1)},
        {"year": 2025, "amount": Decimal("100"), "d": date(2025, 1, 1)},
    ]

    summary = summarizeEvidence(rows)

    # 度量 200 > 100 ⇒ 2024 行在前；按轴（year）降序则 2025 在前
    assert [t["row"]["year"] for t in summary.top_n] == [2024, 2025]
    assert [t["row"]["amount"] for t in summary.top_n] == ["200", "100"]


def test_axis_only_data_yields_no_numeric_digest_instead_of_a_wrong_one() -> None:
    """真时间戳 + 只有一个带时间词的整数轴、**没有**别的数值列 ⇒ 不拿轴当度量。

    宁可 `total`/`baseline`/`current` 为 `None`，也不要 2024/2025 这种「看着合理」的错值
    （2024 + 2025 = 4049 是最坏的错：它看起来像个正常的量）。时间轴本身仍可用。
    """
    rows = [{"year": 2024, "d": date(2024, 1, 1)}, {"year": 2025, "d": date(2025, 1, 1)}]

    summary = summarizeEvidence(rows)

    assert summary.total is None
    assert summary.baseline is None and summary.current is None
    assert summary.top_n == []
    assert summary.time_range == {"start": "2024-01-01", "end": "2025-01-01"}


def test_token_worded_numeric_is_still_the_metric_when_no_real_timestamp() -> None:
    """退回口径：**没有**真时间戳时，带时间词的数值列仍被当作度量（它是唯一候选）。"""
    rows = [{"month": 1, "month_sales": Decimal("5")}, {"month": 2, "month_sales": Decimal("7")}]

    summary = summarizeEvidence(rows)

    assert summary.total == "12"
    assert summary.baseline == "5" and summary.current == "7"


def test_top_n_limit_and_ranking_are_deterministic() -> None:
    rows = [
        {"name": "a", "v": Decimal("5")},
        {"name": "b", "v": Decimal("9")},
        {"name": "c", "v": Decimal("7")},
    ]

    summary = summarizeEvidence(rows, topNLimit=2)

    assert [t["row"]["name"] for t in summary.top_n] == ["b", "c"]
    assert [t["rank"] for t in summary.top_n] == [1, 2]


def test_sample_rows_limit_is_applied() -> None:
    rows = [
        {"name": "a", "v": Decimal("1")},
        {"name": "b", "v": Decimal("2")},
        {"name": "c", "v": Decimal("3")},
    ]

    assert len(summarizeEvidence(rows, maxSampleRows=2).sample_rows) == 2


# ---------------------------------------------------------------------------
# 空行集
# ---------------------------------------------------------------------------


def test_empty_rows_returns_empty_flag_without_raising() -> None:
    summary = summarizeEvidence([])

    assert summary.empty is True
    assert summary.total is None
    assert summary.baseline is None and summary.current is None
    assert summary.delta is None and summary.delta_ratio is None
    assert summary.top_n == [] and summary.sample_rows == []
    assert summary.time_range is None


def test_text_only_rows_have_no_numeric_digest() -> None:
    """无数值列：不聚合、不出基线，但也不抛（`empty` 仅指「零行」）。"""
    summary = summarizeEvidence([{"name": "alice"}, {"name": "bob"}])

    assert summary.empty is False
    assert summary.total is None
    assert summary.top_n == []
    assert summary.baseline is None


# ---------------------------------------------------------------------------
# D9：类型从取值推断；columns 参数可选、只按名字用
# ---------------------------------------------------------------------------


def test_infers_kinds_from_values_not_declared_types() -> None:
    rows = [
        {
            "i": 1,
            "n": Decimal("1.5"),
            "f": 2.5,
            "d": date(2024, 1, 1),
            "t": "x",
            "b": True,
            "u": None,
        }
    ]

    assert inferColumnKinds(rows) == {
        "i": "integer",
        "n": "numeric",
        "f": "numeric",
        "d": "timestamp",
        "t": "text",
        "b": "boolean",
        "u": "unknown",
    }


def test_columns_param_is_optional_and_name_only() -> None:
    """显式传列时只按**名字**用 —— 上游（`stepResult`）只给裸列名，没有 `type` 字段。"""
    rows = [{"year": 2024, "amount": Decimal("100")}, {"year": 2025, "amount": Decimal("200")}]

    summary = summarizeEvidence(rows, list(rows[0].keys()))

    assert summary.baseline == "100" and summary.current == "200"


def test_mixed_type_column_is_unknown_and_not_aggregated() -> None:
    rows = [{"mixed": 1}, {"mixed": "x"}]

    assert inferColumnKinds(rows)["mixed"] == "unknown"
    assert summarizeEvidence(rows).total is None


# ---------------------------------------------------------------------------
# D10：question 死参已删
# ---------------------------------------------------------------------------


def test_question_parameter_is_gone() -> None:
    assert "question" not in inspect.signature(summarizeEvidence).parameters


# ---------------------------------------------------------------------------
# D15 bug 1：float 列不得让求和的 Decimal 累加器炸
# ---------------------------------------------------------------------------


def test_float_metric_does_not_crash_the_sum() -> None:
    rows = [{"month": 1, "ratio": 1.5}, {"month": 2, "ratio": 2.5}]

    summary = summarizeEvidence(rows)

    assert summary.total == "4.0"
    assert summary.baseline == "1.5" and summary.current == "2.5"


# ---------------------------------------------------------------------------
# D15 bug 2：时间列含 NULL 不得让排序比较 date 与 str 而崩
# ---------------------------------------------------------------------------


def test_time_column_with_null_does_not_crash_sort() -> None:
    rows = [
        {"created_at": date(2024, 3, 1), "amount": Decimal("30")},
        {"created_at": None, "amount": Decimal("99")},
        {"created_at": date(2024, 1, 1), "amount": Decimal("10")},
    ]

    summary = summarizeEvidence(rows)

    assert summary.baseline == "10"  # 最早（2024-01-01）
    assert summary.current == "30"  # 最晚（2024-03-01）——NULL 行不参与边界
    assert summary.time_range == {"start": "2024-01-01", "end": "2024-03-01"}


def test_datetime_time_column_sorts_chronologically() -> None:
    rows = [
        {"ts": datetime(2024, 5, 1, 9, 0), "v": Decimal("2")},
        {"ts": datetime(2024, 1, 1, 9, 0), "v": Decimal("1")},
    ]

    summary = summarizeEvidence(rows)

    assert summary.baseline == "1" and summary.current == "2"


# ---------------------------------------------------------------------------
# D15 bug 3：子串匹配会把 `candidate_name` 当成时间列（含 "date"）
# ---------------------------------------------------------------------------


def test_column_named_candidate_name_is_not_a_time_column() -> None:
    rows = [
        {"candidate_name": "alice", "amount": Decimal("10")},
        {"candidate_name": "bob", "amount": Decimal("20")},
    ]

    summary = summarizeEvidence(rows)

    assert summary.time_range is None
    assert summary.baseline is None and summary.current is None
    assert summary.delta is None and summary.delta_ratio is None
    assert summary.total == "30"  # 数值列仍被聚合（不是整条摘要报废）


def test_camel_case_time_column_is_detected_by_word_boundary() -> None:
    rows = [{"createDate": 2024, "v": Decimal("1")}, {"createDate": 2025, "v": Decimal("2")}]

    assert summarizeEvidence(rows).time_range == {"start": "2024", "end": "2025"}


# ---------------------------------------------------------------------------
# D14：JSONB 边界 —— 含 Decimal/date 的摘要真的能落库，且转换是承重的
# ---------------------------------------------------------------------------


def test_evidence_payload_is_jsonb_writable_with_decimal_and_date() -> None:
    rows = [
        {"TS": datetime(2024, 1, 1, 10, 0), "AMOUNT": Decimal("100.50")},
        {"TS": datetime(2024, 2, 1, 10, 0), "AMOUNT": Decimal("150.75")},
    ]

    summary = summarizeEvidence(rows)
    bound = _jsonbSerializer()(summary.model_dump(by_alias=True))
    parsed = json.loads(bound)

    assert parsed["baseline"] == "100.50"
    assert isinstance(parsed["baseline"], str) and not isinstance(parsed["baseline"], float)
    assert parsed["deltaRatio"] == "0.5"
    assert parsed["sampleRows"][0]["AMOUNT"] == "100.50"
    assert parsed["sampleRows"][0]["TS"] == "2024-01-01T10:00:00"
    assert parsed["timeRange"] == {
        "start": "2024-01-01T10:00:00",
        "end": "2024-02-01T10:00:00",
    }


def test_jsonb_safety_conversion_is_load_bearing() -> None:
    """反向断言：同样的驱动标量**不经** `research_json` 直接进 JSONB 会抛线上那记 TypeError。

    这正是 D14 要修的失败类（SQLAlchemy JSONB serializer = `json.dumps`）。删掉
    `summarizeEvidence` 里那次 `research_json` 往返，上一条测试就会以同一异常变红。
    """
    serializer = _jsonbSerializer()

    with pytest.raises(TypeError, match="Decimal is not JSON serializable"):
        serializer({"baseline": Decimal("100.50")})
    with pytest.raises(TypeError, match="not JSON serializable"):
        serializer({"sampleRows": [{"TS": date(2024, 1, 1)}]})


# ---------------------------------------------------------------------------
# D13：frozen CamelModel —— snake 字段 / camel 线上别名 / Task 5 契约
# ---------------------------------------------------------------------------


def test_model_is_frozen() -> None:
    summary = summarizeEvidence([{"v": Decimal("1")}])

    with pytest.raises(ValidationError):
        summary.total = "9"


def test_wire_aliases_are_camel_case_while_python_names_stay_snake() -> None:
    summary = summarizeEvidence([{"year": 2024, "amount": Decimal("1")}])

    wire = summary.model_dump(by_alias=True)
    assert "deltaRatio" in wire and "delta_ratio" not in wire
    assert "topN" in wire and "sampleRows" in wire and "timeRange" in wire
    assert "delta_ratio" in summary.model_dump()


def test_construct_by_python_field_name_normalizes_numerics_to_wire_strings() -> None:
    """Task 5 契约（D13）：`EvidenceSummary(baseline=..., current=..., empty=...)` 必须可用。"""
    summary = EvidenceSummary(baseline=Decimal("100"), current=Decimal("200"), empty=False)

    assert summary.baseline == "100" and summary.current == "200"
    assert summary.model_dump(mode="json", by_alias=True)["baseline"] == "100"


def test_numeric_accessor_orders_correctly_unlike_raw_strings() -> None:
    """数值字段是**字符串**（`research_json` 口径），比较/算术必须走 `numeric()`。

    裸串比较是**字典序**：`'1000' > '900'` 为 `False` —— 会静默反转真实大小，且不报错。
    """
    rows = [{"year": 2024, "amount": Decimal("900")}, {"year": 2025, "amount": Decimal("1000")}]

    summary = summarizeEvidence(rows)

    assert summary.baseline == "900" and summary.current == "1000"
    # 正确读法：走访问器
    assert summary.numeric("current") > summary.numeric("baseline")  # 1000 > 900
    assert summary.numeric("delta") == Decimal("100")
    # 反向断言（钉住「为什么需要访问器」）：裸串是字典序，真实顺序被静默反转
    assert not (summary.current > summary.baseline)  # '1000' > '900' is False


def test_numeric_accessor_returns_none_for_absent_or_non_numeric() -> None:
    empty = summarizeEvidence([])

    assert empty.numeric("baseline") is None  # 缺席字段
    assert empty.numeric("empty") is None  # 非数值（bool）字段
    assert empty.numeric("totally_unknown") is None  # 未声明字段


def test_declared_fields_are_all_computed_and_no_dead_fields() -> None:
    """每个声明字段都要真被算出来（不留恒 None 的死字段），并剔掉无口径的字段。"""
    fields = set(EvidenceSummary.model_fields)

    assert fields == {
        "total",
        "baseline",
        "current",
        "delta",
        "delta_ratio",
        "top_n",
        "sample_rows",
        "time_range",
        "empty",
    }


# ---------------------------------------------------------------------------
# D11/D12：唯一构造点 `stepResult` 加法接线 + summary 仍是 str
# ---------------------------------------------------------------------------


def test_step_result_summary_stays_string_and_carries_evidence() -> None:
    rows = [{"year": 2024, "amount": Decimal("100")}, {"year": 2025, "amount": Decimal("200")}]
    evidence = summarizeEvidence(rows).model_dump(by_alias=True)

    result = stepResult(
        {"index": 0, "description": "d", "sub_question": "q", "sql": "SELECT 1"},
        rows=rows,
        error=None,
        evidence=evidence,
    )

    assert isinstance(result["summary"], str)  # D12 反向断言
    assert result["evidence"] == evidence
    # 整个步结果（summary 是 str、evidence 是 JSON 安全 dict）可落 JSONB
    assert json.loads(_jsonbSerializer()(result))["evidence"]["baseline"] == "100"


def test_step_result_without_evidence_keeps_the_key_none() -> None:
    result = stepResult({"index": 0}, rows=[], error="boom")

    assert isinstance(result["summary"], str)
    assert result["evidence"] is None


def test_step_result_distinguishes_no_rows_from_failed_summarization() -> None:
    """`evidence` 恒存在（可为 `null`）；「无行」与「有行但摘要失败」用 `rowCount` 区分。"""
    noRows = stepResult({"index": 0}, rows=[], error=None)
    assert noRows["evidence"] is None and noRows["rowCount"] == 0

    summarizationFailed = stepResult({"index": 1}, rows=[{"v": Decimal("1")}], error=None, evidence=None)
    assert summarizationFailed["evidence"] is None and summarizationFailed["rowCount"] == 1


# ---------------------------------------------------------------------------
# 出图路径包装：结构化摘要是加法，算不出来不得把单步升级成整 turn 失败
# ---------------------------------------------------------------------------


def test_try_summarize_never_breaks_the_step_on_unsupported_driver_type() -> None:
    rows = [{"blob": b"\x00\x01", "amount": Decimal("1")}]

    with pytest.raises(TypeError):
        summarizeEvidence(rows)  # 硬函数：显式失败（与 research_json 口径一致）

    assert trySummarizeEvidence(rows) is None  # 出图路径：只留痕，不给证据
    assert trySummarizeEvidence([{"v": Decimal("1")}]).total == "1"


# ---------------------------------------------------------------------------
# 修复轮 2：轴 ≠ 度量的判据必须覆盖**非 ASCII 列名**与**标识符列**
#
# 修复轮 1 的判据是「名字里**有没有**时间词」且只认英文整词（`_tokenize` 用 ASCII 切分，
# 中文列名 token 化成 `[]`，永不命中）。正确判据是「名字**是不是纯时间名**」+ 标识符列按
# **后缀**识别。三条错值形态：中文年份轴被当度量（英文同类已修）、id/rank 列被求和、
# 名字含时间词的合法度量被整块丢掉。
# ---------------------------------------------------------------------------


def test_chinese_axis_and_measure_are_separated_in_the_digest() -> None:
    """中文别名（`nl2sql_refine._SAFE_IDENT_RE` 允许 CJK 标识符）同属「整数轴当度量」。

    旧判据只认英文整词 ⇒ `年份` 通过过滤成为度量：`total = 2024 + 2025 = 4049`、
    基线/当前变成**年份** —— 同比报表拿年份比年份，是「看着合理的错数」。
    """
    rows = [
        {"年份": 2024, "销售额": Decimal("100"), "创建日期": date(2024, 1, 1)},
        {"年份": 2025, "销售额": Decimal("200"), "创建日期": date(2025, 1, 1)},
    ]

    summary = summarizeEvidence(rows)

    assert summary.total == "300"  # 不是 4049
    assert summary.baseline == "100" and summary.current == "200"
    assert summary.delta == "100" and summary.delta_ratio == "1"
    assert summary.time_range == {"start": "2024-01-01", "end": "2025-01-01"}


def test_chinese_axis_top_n_ranks_by_measure_not_by_axis() -> None:
    """TopN 也按度量排：本数据下「按轴（年份）降序」与「按度量（销售额）降序」结果相反。"""
    rows = [
        {"年份": 2024, "销售额": Decimal("200"), "创建日期": date(2024, 1, 1)},
        {"年份": 2025, "销售额": Decimal("100"), "创建日期": date(2025, 1, 1)},
    ]

    summary = summarizeEvidence(rows)

    assert [t["row"]["年份"] for t in summary.top_n] == [2024, 2025]
    assert [t["row"]["销售额"] for t in summary.top_n] == ["200", "100"]
    assert summary.baseline == "200" and summary.current == "100"


def test_chinese_pure_time_axis_only_yields_no_digest() -> None:
    """中文版「宁可 None」：真时间戳 + 纯中文时间轴、没有别的数值列 ⇒ 不拿轴当度量。"""
    rows = [
        {"年份": 2024, "创建日期": date(2024, 1, 1)},
        {"年份": 2025, "创建日期": date(2025, 1, 1)},
    ]

    summary = summarizeEvidence(rows)

    assert summary.total is None
    assert summary.baseline is None and summary.current is None
    assert summary.time_range == {"start": "2024-01-01", "end": "2025-01-01"}


def test_identifier_shaped_integer_column_is_not_the_metric_product_id() -> None:
    """`product_id` 无时间词、又是数值列 ⇒ 旧判据把它求和：`101 + 205 = 306`。"""
    rows = [
        {"product_id": 101, "amount": Decimal("50")},
        {"product_id": 205, "amount": Decimal("70")},
    ]

    summary = summarizeEvidence(rows)

    assert summary.total == "120"
    assert summary.total != "306"  # 反向断言：id 列没有被求和


def test_identifier_shaped_integer_column_is_not_the_metric_rank() -> None:
    rows = [{"rank": 1, "amount": Decimal("50")}, {"rank": 2, "amount": Decimal("70")}]

    summary = summarizeEvidence(rows)

    assert summary.total == "120"
    assert summary.total != "3"


def test_chinese_identifier_shaped_column_is_not_the_metric() -> None:
    """中文标识符后缀（`序号`）同样不得被求和 —— 判据不能只认英文后缀。"""
    rows = [{"序号": 1, "amount": Decimal("50")}, {"序号": 2, "amount": Decimal("70")}]

    assert summarizeEvidence(rows).total == "120"


def test_measure_name_containing_a_time_word_is_not_discarded() -> None:
    """修复轮 1 的**反向**回归：`period_cost` / `month_volume` 这类合法 ERP 度量名含时间词。

    判据必须是「名字**是不是纯时间名**」（`period_cost` 有 `cost` ⇒ 不是），而不是
    「名字含不含时间词」—— 后者会把它们整块丢掉（旧代码 `total is None`，修复前是 300）。
    """
    rows = [
        {"d": date(2024, 1, 1), "period_cost": Decimal("100")},
        {"d": date(2025, 1, 1), "period_cost": Decimal("200")},
    ]

    summary = summarizeEvidence(rows)

    assert summary.total == "300"
    assert summary.baseline == "100" and summary.current == "200"


def test_identifier_suffix_discipline_keeps_no_of_orders_as_a_measure() -> None:
    """`no_of_orders` 含 token `no`，但它**不以**标识符词结尾 ⇒ 仍是度量（后缀纪律）。

    若标识符判据误用「包含」/「任一 token 命中」，此列会被丢掉，真时间戳在场时
    `total` 直接变 `None`（与 `period_cost` 同款的错值形态）。
    """
    rows = [
        {"d": date(2024, 1, 1), "no_of_orders": 3},
        {"d": date(2025, 1, 1), "no_of_orders": 4},
    ]

    assert summarizeEvidence(rows).total == "7"


def test_pure_time_name_predicate_matches_units_not_mere_containment() -> None:
    assert _isPureTimeName("year") is True
    assert _isPureTimeName("quarter") is True
    assert _isPureTimeName("年份") is True
    assert _isPureTimeName("月份") is True
    assert _isPureTimeName("period_cost") is False  # 有 `cost`
    assert _isPureTimeName("月度量") is False  # 有 `量`
    assert _isPureTimeName("日志") is False  # 有 `志`


def test_identifier_name_predicate_uses_suffix_not_substring() -> None:
    assert _isIdentifierName("order_no") is True
    assert _isIdentifierName("product_id") is True
    assert _isIdentifierName("rank") is True
    assert _isIdentifierName("序号") is True
    assert _isIdentifierName("no_of_orders") is False  # `no` 是 token，但不在**结尾**
    assert _isIdentifierName("amount") is False


def test_time_token_predicate_whole_word_for_ascii_and_charwise_for_cjk() -> None:
    """英文整词纪律不得因中文支持而退回子串（`candidate_name` 含 `date` 子串，D15 bug 3）。"""
    assert _hasTimeToken("candidate_name") is False
    assert _hasTimeToken("year") is True
    assert _hasTimeToken("period_cost") is True  # 「含」时间词 —— 但它不是**纯**时间名
    assert _hasTimeToken("年份") is True
    assert _hasTimeToken("创建日期") is True
    assert _hasTimeToken("销售额") is False


# ---------------------------------------------------------------------------
# 修复轮 3：整数列的**名字兜底**必须是「纯时间名」，不能是「含时间字」
#
# 修复轮 2 把 `_hasTimeToken` 扩到 CJK **按字**匹配（对 `_isPureTimeName` 是对的），
# 但 `_timeColumn` 的**名字兜底**同时接受 `KIND_INTEGER` ⇒ `_hasTimeToken` 的「含」
# 判据落到整数列上，任何含 `度/时/日/月/年/周/期/间/季` 的整数列都被升格成时间轴，
# 再从度量候选里被排除（`_metricColumn` 只认「纯时间名」，口径不一致）。
#
# 两个后果，**都不是「保守」**：
# 1. 合法整数度量被整块丢掉（`total=None`）—— `满意度`/`活跃度`/`利润度`/`时长` 在本仓
#    业务域里是**成类**的度量名，不是孤例；
# 2. `time_range` 被**编造**（满意度评分 5/3 变成「时间跨度 3→5」）—— 这是一个会被下游
#    渲染给用户看的字段，即「看着合理的错值」，正是本模块最该避免的一类。
#
# 修复只动 `_timeColumn` 的名字兜底：整数列只认 `_isPureTimeName`；文本列（`stat_date`
# 这类**文本**日期轴）保持「包含」判据。前三条是新行为，后五条是回归屏。
# ---------------------------------------------------------------------------


def test_integer_measure_with_a_time_char_is_not_promoted_to_the_time_axis() -> None:
    """`满意度` 含 `度` ⇒ 曾整列被当时间轴，`time_range` 拿**评分**当时间跨度渲染给用户。

    ⚠️ 轴判据放开后 `满意度` 就是**第一个数值候选**（模块契约：`total` = 第一个数值列求和，
    见 task-4-brief 的参考实现），故 `total` 是满意度之和 `'8'`。夹具的 `300` 是**修复前**的
    值（那时 `满意度` 被误当轴、`sales` 才是度量）—— 修复轮 3 简报表格里第 1 行的
    「度量 = sales（total=300）」照抄了修复前的输出；在「只改 `_timeColumn`、不动
    `_metricColumn`」的前提下不可达（详见交付报告）。
    """
    rows = [{"满意度": 5, "sales": 100}, {"满意度": 3, "sales": 200}]

    summary = summarizeEvidence(rows)

    assert summary.time_range is None
    assert summary.total == "8"  # 度量 = 满意度（第一个数值候选）
    assert summary.baseline is None and summary.current is None  # 无时间轴 ⇒ 无基线/当前


def test_lone_integer_measure_with_a_time_char_is_still_digested() -> None:
    """`时长` 单列：名字兜底曾把它选成 `timeColumn` ⇒ 度量候选被排除光 ⇒ `total=None`。"""
    rows = [{"时长": 10}, {"时长": 20}]

    summary = summarizeEvidence(rows)

    assert summary.total == "30"
    assert summary.time_range is None


def test_integer_measure_with_a_time_char_does_not_fabricate_a_time_range() -> None:
    """`日志` 含 `日`：修复前 `time_range` 被编造成 `{'start':'1','end':'2'}`（日志序号）。

    同第 1 行：轴放开后 `日志` 是第一个数值候选 ⇒ `total='3'`（`120` 是修复前的值，那时
    `日志` 被误当轴、`amount` 才是度量）。
    """
    rows = [{"日志": 1, "amount": 50}, {"日志": 2, "amount": 70}]

    summary = summarizeEvidence(rows)

    assert summary.time_range is None
    assert summary.total == "3"  # 度量 = 日志（第一个数值候选）


@pytest.mark.parametrize(
    ("rows", "expected_time_range", "expected_total"),
    [
        pytest.param(
            [{"年份": 2024, "销售额": Decimal("100")}, {"年份": 2025, "销售额": Decimal("200")}],
            {"start": "2024", "end": "2025"},
            "300",
            id="pure-cjk-time-integer-stays-the-axis",
        ),
        pytest.param(
            [{"year": 2024, "amount": Decimal("100")}, {"year": 2025, "amount": Decimal("200")}],
            {"start": "2024", "end": "2025"},
            "300",
            id="pure-ascii-time-integer-stays-the-axis",
        ),
        pytest.param(
            [{"stat_date": "2024-01-01", "amount": Decimal("100")},
             {"stat_date": "2025-01-01", "amount": Decimal("200")}],
            {"start": "2024-01-01", "end": "2025-01-01"},
            "300",
            id="text-date-axis-keeps-the-containment-predicate",
        ),
        pytest.param(
            [{"年份": 2024, "销售额": Decimal("100"), "创建日期": date(2024, 1, 1)},
             {"年份": 2025, "销售额": Decimal("200"), "创建日期": date(2025, 1, 1)}],
            {"start": "2024-01-01", "end": "2025-01-01"},
            "300",
            id="real-timestamp-still-wins-over-the-pure-time-integer",
        ),
        pytest.param(
            [{"year": 2024, "d": date(2024, 1, 1)}, {"year": 2025, "d": date(2025, 1, 1)}],
            {"start": "2024-01-01", "end": "2025-01-01"},
            None,
            id="axis-only-data-still-yields-no-numeric-digest",
        ),
    ],
)
def test_fix3_axis_selection_regression_matrix(
    rows: list[dict[str, object]], expected_time_range: dict[str, str], expected_total: str | None
) -> None:
    """修复轮 2 的成果不得回退：纯时间名仍是轴、文本日期轴仍靠「包含」、真时间戳仍优先。

    `expected_total` 同时钉住**度量列是谁**：`年份`/`year` 若不再是轴就会被求和成
    `2024 + 2025 = 4049`，故 `total == '300'` 是承重断言（不是「随便一个非 None」）。
    """
    summary = summarizeEvidence(rows)

    assert summary.time_range == expected_time_range
    assert summary.total == expected_total


def test_ascii_integer_date_column_is_still_the_axis_and_is_not_summed() -> None:
    """`createDate`：**整词**命中时间词（tokens `create`+`date`）的**整数**日期列。

    整数兜底若只认「纯时间名」，`createDate` 就不再是轴 ⇒ 掉进度量候选被求和 ⇒
    `total` 变成 `2024 + 2025 = 4049`（D16 那记「看着合理的错数」原样重现）。故整数兜底
    保留 ASCII **整词**匹配（CJK 仍只认纯时间名 —— 见下条）。
    """
    rows = [{"createDate": 2024, "v": Decimal("1")}, {"createDate": 2025, "v": Decimal("2")}]

    summary = summarizeEvidence(rows)

    assert summary.time_range == {"start": "2024", "end": "2025"}
    assert summary.total == "3"  # 度量 = v，不是 4049
    assert summary.total != "4049"


def test_time_word_predicate_is_ascii_whole_word_and_cjk_free() -> None:
    """给整数兜底用的「时间整词」判据：ASCII 整词（不是子串），CJK 恒 False。"""
    assert _hasTimeWord("createDate") is True
    assert _hasTimeWord("stat_date") is True
    assert _hasTimeWord("year") is True
    assert _hasTimeWord("candidate_name") is False  # 含 `date` 子串但非整词（D15 bug 3）
    assert _hasTimeWord("满意度") is False  # CJK 无词边界 ⇒ 由 _isPureTimeName 兜
    assert _hasTimeWord("时长") is False


# ---------------------------------------------------------------------------
# 修复轮 4：整数档再加一条**中文两字期间后缀**判据
#
# 修复轮 3 把整数列的兜底收窄成「时间整词（ASCII）或纯时间名」，救回了 ASCII 那一半
# （`createDate`），但 `_hasTimeWord` 对中文**恒 False**（CJK 无词边界），而 `_isPureTimeName`
# 要求**全字皆**时间字 ⇒ `会计年度` / `统计年月` / `会计期间` 这类**带非时间前缀**的中文期间名
# 两头都不命中，掉进度量候选被求和：`total` 变成「年份之和 / 年月编号之和 / 期间号之和」
# （D16 那记「看着合理的错数」，只是从 `time_range` 搬到了 `total`）。
#
# 新判据 = 旧判据 **∨** 「中文期间词后缀」，是**单调加宽**：只增加能当轴的列，不可能让
# 现在正确的列失去轴资格。
#
# ⚠️ 修复轮 5 已把这条判据从「末两字是时间**字**」收窄成「末两字是**期间词**」闭集 —— 前者
# 会把 `工作时间`/`响应时间` 一并收成轴（见本文件末尾「修复轮 5」段）。本段保留的是
# 「带非时间前缀的中文期间名仍是轴」这一**回归屏**。
# ---------------------------------------------------------------------------


def test_chinese_period_word_predicate_matches_closed_set_suffixes() -> None:
    """`_hasPeriodWord`：名字以**中文期间词**结尾（闭集）⇒ 期间形名字。

    ⚠️ 刻意**不含** `时间` 与 `周期`：整数列里「…时间」是时长（`工作时间`/`响应时间`）、
    `周期` 是周期天数 —— 它们是**度量**，不是轴（`周期` 仍靠 `_isPureTimeName` 当轴，见 N5）。
    """
    # 正例：以期间词结尾（含带非时间前缀的期间名）
    assert _hasPeriodWord("会计年度") is True
    assert _hasPeriodWord("统计年月") is True
    assert _hasPeriodWord("会计期间") is True
    assert _hasPeriodWord("年份") is True  # 恰好是 `年份`
    assert _hasPeriodWord("期间") is True
    # 反例：不以期间词结尾
    assert _hasPeriodWord("工作时间") is False  # 末两字 `时间` —— 刻意排除（度量）
    assert _hasPeriodWord("周期") is False  # `周期` 刻意排除
    assert _hasPeriodWord("报表期") is False  # 末两字 `表期`
    assert _hasPeriodWord("时长") is False
    assert _hasPeriodWord("满意度") is False
    assert _hasPeriodWord("") is False


def test_period_shaped_predicate_matches_year_yearmonth_yearmonthday() -> None:
    """`_isPeriodShaped`：整年（1900–2100）/ yyyymm / yyyymmdd；只看 int，``bool`` 不算。"""
    # 正例：三种期间形状
    assert _isPeriodShaped([2024, 2025]) is True
    assert _isPeriodShaped([202401, 202501]) is True
    assert _isPeriodShaped([20240101]) is True
    assert _isPeriodShaped([20240101, 20251231]) is True
    # 反例：非期间形状
    assert _isPeriodShaped([5, 3]) is False  # 评分
    assert _isPeriodShaped([8, 10]) is False  # 时长
    assert _isPeriodShaped([300, 500]) is False  # 时长（秒）
    assert _isPeriodShaped([202413]) is False  # 13 月 —— yyyymm 要求月 1..12
    assert _isPeriodShaped(["2024"]) is False  # 字符串不算
    assert _isPeriodShaped([2024.0]) is False  # float 不算
    assert _isPeriodShaped([True]) is False  # bool 不算（虽为 int 子类）
    assert _isPeriodShaped([]) is False  # 空
    assert _isPeriodShaped([None]) is False  # 全缺失
    assert _isPeriodShaped([2024, 5]) is False  # 混形状（整年 + 短数）不成立


def test_fiscal_year_integer_axis_is_restored_and_not_summed() -> None:
    """C7：`会计年度`（末两字 `年度` ⇒ 期间名）必须仍是轴，`total` 是销售额之和 `300`。

    修复前（`bc7347f`）：`会计年度` 掉进度量候选 ⇒ `total = 2024 + 2025 = 4049`、
    `time_range=None`（年份轴丢失）。反向断言钉住那记错值。
    """
    rows = [
        {"会计年度": 2024, "销售额": Decimal("100")},
        {"会计年度": 2025, "销售额": Decimal("200")},
    ]

    summary = summarizeEvidence(rows)

    assert summary.time_range == {"start": "2024", "end": "2025"}
    assert summary.total == "300"
    assert summary.total != "4049"  # 反向断言：年份没有被求和
    assert summary.baseline == "100" and summary.current == "200"


def test_stat_yearmonth_integer_axis_is_restored_and_not_summed() -> None:
    """C8：`统计年月`（末两字 `年月`）—— 修复前 `total = 202401 + 202501 = 404902`。"""
    rows = [
        {"统计年月": 202401, "销售额": Decimal("100")},
        {"统计年月": 202501, "销售额": Decimal("200")},
    ]

    summary = summarizeEvidence(rows)

    assert summary.time_range == {"start": "202401", "end": "202501"}
    assert summary.total == "300"
    assert summary.total != "404902"  # 反向断言：年月编号没有被求和
    assert summary.baseline == "100" and summary.current == "200"


def test_accounting_period_integer_axis_is_restored_and_not_summed() -> None:
    """C9：`会计期间`（末两字 `期间`）—— 修复前 `total = 1 + 2 = 3`（期间号之和）。"""
    rows = [
        {"会计期间": 1, "销售额": Decimal("100")},
        {"会计期间": 2, "销售额": Decimal("200")},
    ]

    summary = summarizeEvidence(rows)

    assert summary.time_range == {"start": "1", "end": "2"}
    assert summary.total == "300"
    assert summary.total != "3"  # 反向断言：期间号没有被求和
    assert summary.baseline == "100" and summary.current == "200"


def test_integer_measure_with_two_trailing_time_chars_is_still_a_measure() -> None:
    """回归屏（C11）：`完成率` 末两字 `成率` 不在时间字集合 ⇒ 仍是度量，不被升格成轴。

    与 `满意度`/`时长`/`日志` 同形：含时间**字**的整数度量不得因新判据被误当期间名。
    """
    rows = [{"完成率": 90, "amount": Decimal("50")}, {"完成率": 85, "amount": Decimal("70")}]

    summary = summarizeEvidence(rows)

    assert summary.time_range is None
    assert summary.total == "175"  # 度量 = 完成率（第一个数值候选），不是 amount 之和 120


def test_period_suffix_widening_does_not_steal_ascii_integer_date_axis() -> None:
    """回归屏：`createDate` / `create_date`（ASCII 整词日期轴）不受新判据影响，仍不被求和。"""
    camel = summarizeEvidence(
        [{"createDate": 2024, "v": Decimal("1")}, {"createDate": 2025, "v": Decimal("2")}]
    )
    snake = summarizeEvidence(
        [
            {"create_date": 20240101, "amount": Decimal("100")},
            {"create_date": 20250101, "amount": Decimal("200")},
        ]
    )

    assert camel.time_range == {"start": "2024", "end": "2025"} and camel.total == "3"
    assert snake.time_range == {"start": "20240101", "end": "20250101"} and snake.total == "300"


# ---------------------------------------------------------------------------
# 修复轮 5：整数轴的**期间语义**改由「名字含时间词 **且** 取值像期间」判定
#
# 轮 3 收窄（整数只认「时间整词 ∨ 纯时间名」）救回 ASCII 日期轴，却把带非时间前缀的
# 中文期间名（`会计年度`/`统计年月`/`会计期间`）漏成度量；轮 4 的「末两字是时间字」
# 又把 `工作时间`/`响应时间`/`持续时间`/`加班时间`（末两字 `时间`）一并收成轴 ——
# 既丢合法度量（`total` 变 None）又把 `time_range` **编造**成时长跨度。
#
# `工作时间` 与 `统计时间` 在**名字**上不可分（非时间前缀 + 时间后缀），故本轮把判据
# 来源换成**取值形状**：期间轴的取值是整年 / yyyymm / yyyymmdd，而时长度量是 8/10、
# 300/500 ⇒ 用**合取**（含时间词 **且** 取值像期间）把两类分开。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "first", "second", "expected_total"),
    [
        ("工作时间", 8, 10, "18"),
        ("响应时间", 300, 500, "800"),
        ("持续时间", 12, 30, "42"),
        ("加班时间", 2, 5, "7"),
    ],
)
def test_fix5_integer_duration_measure_is_not_promoted_to_the_axis(
    name: str, first: int, second: int, expected_total: str
) -> None:
    """N1–N4：末两字是 `时间` 的**整数度量**不得被当轴（不编造 `time_range`，不丢度量）。

    修复前（`e645bc4`）：`…时间` 命中轮 4 的「末两字皆时间字」⇒ 被当轴 ⇒ `total=None`
    且 `time_range` 变成它的取值跨度（把时长渲染成「数据的时间跨度」）。
    """
    rows = [{name: first, "amount": 1}, {name: second, "amount": 2}]

    summary = summarizeEvidence(rows)

    assert summary.time_range is None
    assert summary.total == expected_total  # 度量 = 该列自身（第一个数值候选）


def test_fix5_statistical_time_integer_axis_is_restored_by_value_shape() -> None:
    """N8：`统计时间` 末两字是 `时间`（不在期间词闭集），但取值 202401/202501 像 yyyymm ⇒ 仍是轴。

    这正是「判据来源换成取值」的**承重**用例：单看名字 `统计时间` 与 `工作时间` 同形，
    只有**取值形状**能把它们分开 —— 故名字判据必须**合取**取值判据，缺一不可。
    """
    rows = [{"统计时间": 202401, "amount": 1}, {"统计时间": 202501, "amount": 2}]

    summary = summarizeEvidence(rows)

    assert summary.time_range == {"start": "202401", "end": "202501"}
    assert summary.total == "3"  # 度量 = amount，不是年月编号之和 404902


def test_fix5_period_shaped_values_alone_do_not_make_an_axis() -> None:
    """反向断言：取值恰在 1900–2100 但**名字不含时间词**的整数度量不得被当轴。

    `单价` 1999/2001 单独看「像整年」，但 `单价` 不含时间词 ⇒ 合取不成立 ⇒ 不是轴。
    证明「取值像期间」必须**与**「名字含时间词」合取，而不是单独成立。
    """
    rows = [{"单价": 1999, "amount": 1}, {"单价": 2001, "amount": 2}]

    summary = summarizeEvidence(rows)

    assert summary.time_range is None
    assert summary.total == "4000"  # 度量 = 单价（第一个数值候选）


@pytest.mark.parametrize(
    ("case", "rows", "expected_time_range", "expected_total"),
    [
        pytest.param(
            "C1", [{"满意度": 5, "sales": 100}, {"满意度": 3, "sales": 200}], None, "8", id="C1"
        ),
        pytest.param("C2", [{"时长": 10}, {"时长": 20}], None, "30", id="C2"),
        pytest.param(
            "C3", [{"日志": 1, "amount": 50}, {"日志": 2, "amount": 70}], None, "3", id="C3"
        ),
        pytest.param(
            "C7",
            [{"会计年度": 2024, "销售额": 100}, {"会计年度": 2025, "销售额": 200}],
            {"start": "2024", "end": "2025"},
            "300",
            id="C7",
        ),
        pytest.param(
            "C8",
            [{"统计年月": 202401, "销售额": 100}, {"统计年月": 202501, "销售额": 200}],
            {"start": "202401", "end": "202501"},
            "300",
            id="C8",
        ),
        pytest.param(
            "C9",
            [{"会计期间": 1, "销售额": 100}, {"会计期间": 2, "销售额": 200}],
            {"start": "1", "end": "2"},
            "300",
            id="C9",
        ),
        pytest.param(
            "C10",
            [{"create_date": 20240101, "amount": 1}, {"create_date": 20250101, "amount": 2}],
            {"start": "20240101", "end": "20250101"},
            "3",
            id="C10",
        ),
        pytest.param(
            "C11", [{"完成率": 90, "amount": 1}, {"完成率": 85, "amount": 2}], None, "175", id="C11"
        ),
        pytest.param(
            "C12",
            [{"年份": 2024, "销售额": 100}, {"年份": 2025, "销售额": 200}],
            {"start": "2024", "end": "2025"},
            "300",
            id="C12",
        ),
        pytest.param(
            "N1", [{"工作时间": 8, "amount": 1}, {"工作时间": 10, "amount": 2}], None, "18", id="N1"
        ),
        pytest.param(
            "N2",
            [{"响应时间": 300, "amount": 1}, {"响应时间": 500, "amount": 2}],
            None,
            "800",
            id="N2",
        ),
        pytest.param(
            "N3", [{"持续时间": 12, "amount": 1}, {"持续时间": 30, "amount": 2}], None, "42", id="N3"
        ),
        pytest.param(
            "N4", [{"加班时间": 2, "amount": 1}, {"加班时间": 5, "amount": 2}], None, "7", id="N4"
        ),
        # N5：`周期` **不是**本轮目标 —— `_isPureTimeName("周期")` 为 True，三个版本都当轴，
        # 语义本就两可，动它会重开修复轮 1 的 `period_cost` 类回归 ⇒ 本轮维持现状（记进残余限制）。
        pytest.param(
            "N5", [{"周期": 3, "amount": 1}, {"周期": 5, "amount": 2}],
            {"start": "3", "end": "5"}, "3", id="N5",
        ),
        pytest.param(
            "N7", [{"有效期": 12, "amount": 1}, {"有效期": 24, "amount": 2}], None, "36", id="N7"
        ),
        pytest.param(
            "N10", [{"逾期天数": 3, "amount": 1}, {"逾期天数": 5, "amount": 2}], None, "8", id="N10"
        ),
        pytest.param(
            "N8",
            [{"统计时间": 202401, "amount": 1}, {"统计时间": 202501, "amount": 2}],
            {"start": "202401", "end": "202501"},
            "3",
            id="N8",
        ),
        # N6：`报表期` **是已知残余**（末字 `期` 是时间字，但取值 1/2 不像期间；名字又与
        # 「评分/序号」类度量同形）⇒ 判为度量，本轮**刻意不救**（见模块头「残余限制」）。
        pytest.param(
            "N6", [{"报表期": 1, "amount": 1}, {"报表期": 2, "amount": 2}], None, "3", id="N6"
        ),
        # N11/N12：修复轮 6 的目标 —— 名字的时间字只是**前缀**（`月`+`销量` / `月`+`份额`），
        # 末字门收窄后掉回度量。
        pytest.param(
            "N11",
            [{"月销量": 2024, "amount": 1}, {"月销量": 2025, "amount": 2}],
            None,
            "4049",
            id="N11",
        ),
        pytest.param(
            "N12",
            [{"月份额": 202401, "amount": 1}, {"月份额": 202501, "amount": 2}],
            None,
            "404902",
            id="N12",
        ),
        # G1：取值恰落在 1900–2100（`单价` 1999/2001「像整年」），但名字的时间字门不成立
        # ⇒ 仍是度量。（简报表格此处写的 `3` 是「另一列之和」；轴资格从未落在 `单价` 上，
        # 它是**第一个数值候选** ⇒ `total` 是它自身的和 `4000`，见交付报告。）
        pytest.param(
            "G1", [{"单价": 1999, "amount": 1}, {"单价": 2001, "amount": 2}], None, "4000", id="G1"
        ),
    ],
)
def test_fix5_before_after_table(
    case: str,
    rows: list[dict[str, object]],
    expected_time_range: dict[str, str] | None,
    expected_total: str,
) -> None:
    """21 例对照表的**修复后**期望（轴例度量是另一数值列；度量例度量是该列自身）。

    N1–N4/N7/N10/N11/N12/G1 的 `total` 是**该列自身**之和（轴资格撤掉后它是第一个数值
    候选，与 r3 C1 `满意度` 同款）—— 简报表格里 N11/N12/G1 写的「另一列之和 `3`」在
    「只改 `_timeColumn`、不动 `_metricColumn`」的前提下不可达。
    """
    summary = summarizeEvidence(rows)

    assert summary.time_range == expected_time_range, case
    assert summary.total == expected_total, case


# ---------------------------------------------------------------------------
# 修复轮 6（**最后一轮**）：取值档的名字门从「**含**时间词」收窄为「**末字**是时间字」
#
# 轮 5 用 `_hasTimeToken(name)`（**含**时间词）当取值档的名字门 ⇒ 任何**名字含时间字、取值
# 又恰好像期间**的**整数度量**被升格成轴（`time_range` 被**编造**成它的取值跨度）。中文的
# 期间词是**中心词、居末**（`会计`+`年度`），而 `月销量`（`月` 是前缀修饰，中心词是 `销量`）
# 与 `月份额`（中心词 `份额`）的时间字只是**前缀**。故名字门收窄为**末字**：
# `会计年度`（末字 `度`）/ `统计时间`（末字 `间`）仍是轴；`月销量`（末字 `量`）/
# `月份额`（末字 `额`）掉回度量。取值档的**合取**（取值像期间）一字未动。
# ---------------------------------------------------------------------------


def test_fix6_month_volume_measure_is_not_promoted_to_the_axis() -> None:
    """N11：`月销量` 2024/2025 —— `月` 是**前缀修饰**（中心词 `销量`），取值恰是整年。

    轮 5 的「含时间词 ∧ 取值像期间」把这类整数**度量**升格成轴：`time_range` 被编造成
    `{'2024','2025'}`，把销量渲染成「数据的时间跨度」。末字门（`量` 不在时间字集合）后掉回
    度量。承重断言是 `time_range is None` —— 反向断言钉住那记编造的跨度。
    """
    rows = [{"月销量": 2024, "amount": 1}, {"月销量": 2025, "amount": 2}]

    summary = summarizeEvidence(rows)

    assert summary.time_range is None  # 不是 {'2024','2025'}
    # `total` 是该列**自身**之和（第一个数值候选），不是 `amount` 之和 `3`
    assert summary.total == "4049"
    assert summary.total != "3"


def test_fix6_month_share_measure_is_not_promoted_to_the_axis() -> None:
    """N12：`月份额` 202401/202501 —— 取值恰是 yyyymm，但中心词是 `份额`（度量）。"""
    rows = [{"月份额": 202401, "amount": 1}, {"月份额": 202501, "amount": 2}]

    summary = summarizeEvidence(rows)

    assert summary.time_range is None  # 不是 {'202401','202501'}
    assert summary.total == "404902"
    assert summary.total != "3"


def test_fix6_last_char_gate_keeps_statistical_time_as_the_axis() -> None:
    """末字门**不得**放宽回归：`统计时间` 末字 `间`（时间字）∧ 取值像 yyyymm ⇒ 仍是轴。

    这一行**不得**被新语义放宽（简报红字）：`统计时间` 与 `月销量` 的区别恰在**末字**。
    """
    rows = [{"统计时间": 202401, "amount": 1}, {"统计时间": 202501, "amount": 2}]

    summary = summarizeEvidence(rows)

    assert summary.time_range == {"start": "202401", "end": "202501"}
    assert summary.total == "3"


def test_fix6_empty_column_name_does_not_raise() -> None:
    """`bool(name)` 守卫：空列名不得让取值档的名字门 `name[-1]` 抛 `IndexError`。"""
    rows = [{"": 2024, "amount": 1}, {"": 2025, "amount": 2}]

    summary = summarizeEvidence(rows)

    assert summary.time_range is None
    assert summary.total == "4049"  # 空名是第一个数值候选（无轴资格）
