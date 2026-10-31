"""报告分节解析（2026-10-31）：纯函数、无 IO、绝不抛错。"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.report_sections import buildReportSections, chartSteps


def _step(index: int, **overrides):
    base = {
        "stepIndex": index,
        "description": f"步骤{index + 1}描述",
        "sql": "SELECT 1",
        "chartType": "bar",
        "chartOption": {"series": []},
    }
    base.update(overrides)
    return base


def test_splits_sections_and_places_chart_after_each_section() -> None:
    answer = (
        "## 分析报告\n"
        "### 步骤 1：2024 销售额\n"
        "2024 年销售额 1000 万。\n"
        "\n"
        "### 步骤 2：2025 销售额\n"
        "2025 年销售额 1200 万。\n"
        "\n"
        "### 结论\n"
        "同比增长 20%。"
    )

    blocks = buildReportSections(answer, [_step(0), _step(1)])

    assert blocks == [
        {"kind": "text", "content": "## 分析报告\n### 步骤 1：2024 销售额\n2024 年销售额 1000 万。"},
        {"kind": "chart", "stepIndex": 0, "title": "步骤1描述"},
        {"kind": "text", "content": "### 步骤 2：2025 销售额\n2025 年销售额 1200 万。"},
        {"kind": "chart", "stepIndex": 1, "title": "步骤2描述"},
        {"kind": "text", "content": "### 结论\n同比增长 20%。"},
    ]


def test_accepts_step_keyword_fullwidth_colon_and_heading_levels() -> None:
    answer = "## 分析报告\n#### Step 2：2025 销售额\n增长 20%。"

    blocks = buildReportSections(answer, [_step(1)])

    assert [b["kind"] for b in blocks] == ["text", "chart"]
    assert blocks[1]["stepIndex"] == 1


def test_failed_step_heading_stays_text_and_gets_no_chart() -> None:
    answer = (
        "### 步骤 1：2024 销售额\n1000 万。\n"
        "### 步骤 2：2025 销售额\n本步失败。"
    )
    failed = _step(1, sql=None, chartType=None, chartOption=None)

    blocks = buildReportSections(answer, [_step(0), failed])

    assert [b["kind"] for b in blocks] == ["text", "chart", "text"]
    assert [b.get("stepIndex") for b in blocks if b["kind"] == "chart"] == [0]


def test_duplicate_reference_inserts_chart_once() -> None:
    answer = "### 步骤 1：A\n一。\n### 步骤 1：再看 A\n二。"

    blocks = buildReportSections(answer, [_step(0)])

    assert [b["kind"] for b in blocks] == ["text", "chart", "text"]
    assert len([b for b in blocks if b["kind"] == "chart"]) == 1


def test_unreferenced_earlier_step_is_still_appended() -> None:
    """规则 4：只写 Step 2 时，Step 1 的图不能静默丢失（关键图表补齐）。"""
    answer = "### 步骤 2：B\n只有第二步。"
    blocks = buildReportSections(answer, [_step(0), _step(1)])

    assert [b["kind"] for b in blocks] == ["text", "chart", "text", "chart"]
    assert blocks[1] == {"kind": "chart", "stepIndex": 1, "title": "步骤2描述"}
    assert blocks[2] == {"kind": "text", "content": "### 关键图表"}
    assert blocks[3] == {"kind": "chart", "stepIndex": 0, "title": "步骤1描述"}


def test_unreferenced_chart_steps_are_appended_under_key_charts() -> None:
    answer = "### 步骤 1：A\n一。"

    blocks = buildReportSections(answer, [_step(0), _step(1)])

    assert blocks[-2] == {"kind": "text", "content": "### 关键图表"}
    assert blocks[-1] == {"kind": "chart", "stepIndex": 1, "title": "步骤2描述"}


def test_falls_back_to_text_plus_charts_when_no_section_heading() -> None:
    blocks = buildReportSections("直接给结论。", [_step(0), _step(1)])

    assert blocks == [
        {"kind": "text", "content": "直接给结论。"},
        {"kind": "chart", "stepIndex": 0, "title": "步骤1描述"},
        {"kind": "chart", "stepIndex": 1, "title": "步骤2描述"},
    ]


def test_empty_input_or_no_chart_steps_returns_empty() -> None:
    assert buildReportSections("", [_step(0)]) == []
    assert buildReportSections("   ", [_step(0)]) == []
    assert buildReportSections("纯文字", []) == []
    assert buildReportSections("纯文字", None) == []


def test_supports_snake_case_objects() -> None:
    step = SimpleNamespace(
        step_index=0,
        description="2024 销售额",
        sql="SELECT 1",
        chart_type="bar",
        chart_option={"series": []},
    )

    blocks = buildReportSections("### 步骤 1：2024\n1000 万。", [step])

    assert blocks[1] == {"kind": "chart", "stepIndex": 0, "title": "2024 销售额"}


# ---------------------------------------------------------------------------
# chartSteps：导出分节插图按 stepIndex 取该步的图负载。与 buildReportSections
# 共用「有图」判据，避免两处漂移（一处出块、一处取图，判据必须同一把尺）。
# ---------------------------------------------------------------------------


def test_chart_steps_keeps_only_steps_that_have_a_chart() -> None:
    steps = [
        _step(0),
        _step(1, sql=None, chartType=None, chartOption=None),
        _step(2),
    ]

    assert chartSteps(steps) == [
        {"stepIndex": 0, "chartType": "bar", "chartOption": {"series": []}},
        {"stepIndex": 2, "chartType": "bar", "chartOption": {"series": []}},
    ]


def test_chart_steps_is_sorted_by_step_index() -> None:
    assert [s["stepIndex"] for s in chartSteps([_step(2), _step(0)])] == [0, 2]


def test_chart_steps_supports_snake_case_objects() -> None:
    step = SimpleNamespace(
        step_index=3,
        sql="SELECT 1",
        chart_type="kpi",
        chart_option={"kpi": {"label": "x"}},
    )

    assert chartSteps([step]) == [
        {"stepIndex": 3, "chartType": "kpi", "chartOption": {"kpi": {"label": "x"}}}
    ]


def test_chart_steps_returns_empty_for_non_sequence_input() -> None:
    assert chartSteps(None) == []
    assert chartSteps("not steps") == []
    assert chartSteps([]) == []
