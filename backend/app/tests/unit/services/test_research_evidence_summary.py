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
    _hasTimeToken,
    _isIdentifierName,
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
