"""治理提示文案拼装（纯函数）。"""

from __future__ import annotations

from app.services import messages_zh
from app.services.field_hint_text import (
    appendFieldHints,
    columnLabelsOf,
    composeFieldHintText,
    fieldHintsOf,
    hintsFromSteps,
    mergeFieldHints,
)


def test_empty_hints_produce_no_text():
    assert composeFieldHintText({}) == ""
    assert composeFieldHintText({"missingLabels": [], "missingUnits": []}) == ""


def test_both_kinds():
    text = composeFieldHintText(
        {"missingLabels": ["a"], "missingUnits": ["b"]},
        columnLabels={"b": "收货数量"},
    )
    assert text.startswith("\n\n")
    assert messages_zh.MSG_FIELD_HINT_TITLE in text
    assert "a" in text
    assert "收货数量" in text


def test_truncates_after_eight():
    fields = [f"f{i}" for i in range(10)]
    text = composeFieldHintText({"missingLabels": fields, "missingUnits": []})
    assert "f7" in text
    assert "f8" not in text
    assert "等 10 个" in text


def test_merge_deduplicates_preserving_order():
    merged = mergeFieldHints(
        {"missingLabels": ["a", "b"], "missingUnits": ["x"]},
        {"missingLabels": ["b", "c"], "missingUnits": ["x", "y"]},
    )
    assert merged == {"missingLabels": ["a", "b", "c"], "missingUnits": ["x", "y"]}


def test_append_keeps_answer_when_no_hints():
    assert appendFieldHints("正文", {}) == "正文"


def test_field_hints_of_ignores_malformed():
    assert fieldHintsOf({"fieldHints": {"missingLabels": ["a"]}}) == {
        "missingLabels": ["a"],
        "missingUnits": [],
    }
    assert fieldHintsOf({"fieldHints": "nope"}) is None
    assert fieldHintsOf(None) is None


def test_column_labels_merges_table_and_chart():
    labels = columnLabelsOf(
        {"columnLabels": {"a": "甲"}},
        {"columnLabels": {"b": "乙", "a": "甲2"}},
    )
    assert labels == {"a": "甲2", "b": "乙"}


def test_hints_from_steps_reads_chart_and_table():
    class Step:
        def __init__(self, chart_option, table_option):
            self.chart_option = chart_option
            self.table_option = table_option

    hints, labels = hintsFromSteps(
        [
            Step(
                {"fieldHints": {"missingLabels": ["a"], "missingUnits": []},
                 "columnLabels": {"b": "乙"}},
                {"columnLabels": {"c": "丙"}},
            ),
            Step(
                {"fieldHints": {"missingLabels": [], "missingUnits": ["d"]},
                 "columnLabels": {}},
                None,
            ),
        ]
    )
    assert hints == {"missingLabels": ["a"], "missingUnits": ["d"]}
    assert labels == {"b": "乙", "c": "丙"}
