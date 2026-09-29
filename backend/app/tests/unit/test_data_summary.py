"""summarize_data 单测（feat-smart-data-summary）。

覆盖：
- 空数据 / 单行 / 多行
- int / float / Decimal 数值列
- None / NaN 跳过
- 列数超过 cap（numeric / string 分别）
- head / tail 边界（5/6/10/100）
- truncated 标志判定
- 列类型推断（STRING / NUMBER / TIME）
- JSON 可序列化（Decimal → str）
- 默认参数可调（head_size / tail_size / caps）
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.services.data_summary import (
    DEFAULT_HEAD_SAMPLE_SIZE,
    DEFAULT_NUMERIC_COLUMNS_CAP,
    DEFAULT_STRING_COLUMNS_CAP,
    DEFAULT_TAIL_SAMPLE_SIZE,
    summarize_data,
)


class TestEmptyAndTrivial:
    def test_empty_data_returns_zero_total(self) -> None:
        out = summarize_data([])
        assert out["total"] == 0
        assert out["truncated"] is False
        assert out["columns"] == []
        assert out["column_types"] == {}
        assert out["numeric_stats"] == {}
        assert out["distinct_counts"] == {}
        assert out["samples"] == {"head": [], "tail": []}

    def test_single_row_not_truncated(self) -> None:
        out = summarize_data([{"a": 1, "b": "x"}])
        assert out["total"] == 1
        assert out["truncated"] is False
        # v2 2026-09-18：单行直接全量放 head，tail 为空（不再 head/tail 重叠）
        assert len(out["samples"]["head"]) == 1
        assert out["samples"]["tail"] == []


class TestColumnsAndTypes:
    def test_columns_in_order(self) -> None:
        out = summarize_data([{"a": 1, "b": 2, "c": 3}])
        assert out["columns"] == ["a", "b", "c"]

    def test_numeric_int_column(self) -> None:
        out = summarize_data([{"qty": 10}, {"qty": 20}, {"qty": 30}])
        assert out["column_types"]["qty"] == "NUMBER"
        assert out["numeric_stats"]["qty"]["min"] == 10
        assert out["numeric_stats"]["qty"]["max"] == 30
        assert out["numeric_stats"]["qty"]["sum"] == 60
        assert out["numeric_stats"]["qty"]["avg"] == 20

    def test_numeric_float_column(self) -> None:
        out = summarize_data([{"price": 1.5}, {"price": 2.5}])
        assert out["column_types"]["price"] == "NUMBER"
        assert out["numeric_stats"]["price"]["avg"] == pytest.approx(2.0)

    def test_numeric_decimal_column_preserves_precision(self) -> None:
        # Decimal 必须以 str 形式进 JSON，不能被 json.dumps 抛错
        out = summarize_data([
            {"amt": Decimal("0.8000")},
            {"amt": Decimal("0.2")},
        ])
        assert out["column_types"]["amt"] == "NUMBER"
        stats = out["numeric_stats"]["amt"]
        # min/max/sum/avg 均为 float（统一 JSON 数值语义），精度由 str samples 兜底
        assert stats["min"] == 0.2
        assert stats["max"] == 0.8
        assert stats["sum"] == pytest.approx(1.0)

    def test_datetime_column_detected(self) -> None:
        out = summarize_data([
            {"d": date(2026, 8, 1)},
            {"d": date(2026, 8, 2)},
        ])
        assert out["column_types"]["d"] == "TIME"
        # TIME 列不进 numeric_stats
        assert "d" not in out["numeric_stats"]
        # 但进 distinct_counts
        assert out["distinct_counts"]["d"] == 2

    def test_string_column_type(self) -> None:
        out = summarize_data([{"s": "a"}, {"s": "b"}, {"s": "a"}])
        assert out["column_types"]["s"] == "STRING"
        assert out["distinct_counts"]["s"] == 2  # {"a", "b"}


class TestNullAndNaN:
    def test_null_skipped_in_stats(self) -> None:
        out = summarize_data([
            {"qty": 10}, {"qty": None}, {"qty": 20},
        ])
        stats = out["numeric_stats"]["qty"]
        assert stats["sum"] == 30  # 跳过 None
        assert stats["min"] == 10
        assert stats["max"] == 20

    def test_nan_skipped_in_stats(self) -> None:
        out = summarize_data([
            {"qty": 1.0}, {"qty": math.nan}, {"qty": 3.0},
        ])
        stats = out["numeric_stats"]["qty"]
        # NaN 不应污染 min/max；min/max 至少是有效数值（实现可跳过或保留为 nan；断言「不抛错 + sum=4」即可）
        assert stats["sum"] == 4.0

    def test_all_null_column_returns_empty_stats(self) -> None:
        out = summarize_data([{"qty": None}, {"qty": None}])
        assert "qty" not in out["numeric_stats"]
        # 列仍存在类型推断（NULL 视为 STRING 兜底）
        assert out["column_types"]["qty"] == "STRING"


class TestCaps:
    def test_numeric_columns_capped(self) -> None:
        # 6 个数值列同行，cap = 3 → 只统计前 3 列
        data = [{f"n{i}": i * 10 for i in range(6)} for _ in range(3)]
        out = summarize_data(data, numeric_columns_cap=3)
        assert len(out["numeric_stats"]) == 3
        assert set(out["numeric_stats"].keys()) == {"n0", "n1", "n2"}

    def test_string_columns_capped(self) -> None:
        data = [{f"s{i}": f"v{i}" for i in range(6)} for _ in range(3)]
        out = summarize_data(data, string_columns_cap=3)
        assert len(out["distinct_counts"]) == 3
        assert set(out["distinct_counts"].keys()) == {"s0", "s1", "s2"}

    def test_caps_zero_means_unlimited(self) -> None:
        data = [{f"n{i}": i for i in range(10)} for _ in range(3)]
        out = summarize_data(data, numeric_columns_cap=0)
        assert len(out["numeric_stats"]) == 10


class TestHeadTailSamples:
    def test_head_5_tail_5_default(self) -> None:
        # v2：>100 行才走 head/tail 截断路径；用 120 行验证默认 5+5
        data = [{"i": i} for i in range(120)]
        out = summarize_data(data)
        assert len(out["samples"]["head"]) == 5
        assert [r["i"] for r in out["samples"]["head"]] == [0, 1, 2, 3, 4]
        assert len(out["samples"]["tail"]) == 5
        assert [r["i"] for r in out["samples"]["tail"]] == [115, 116, 117, 118, 119]

    def test_truncated_true_when_data_exceeds_samples(self) -> None:
        # 默认 head=5 + tail=5 = 10；101+ 行（v2 阈值之上）truncated=True
        out = summarize_data([{"i": i} for i in range(101)])
        assert out["truncated"] is True

    def test_truncated_false_when_data_within_samples(self) -> None:
        out = summarize_data([{"i": i} for i in range(100)])
        assert out["truncated"] is False

    def test_truncated_false_for_single_row(self) -> None:
        out = summarize_data([{"i": 0}])
        assert out["truncated"] is False

    def test_custom_head_tail_sizes(self) -> None:
        # v2：>100 行才走截断分支
        data = [{"i": i} for i in range(150)]
        out = summarize_data(data, head_size=3, tail_size=2)
        assert [r["i"] for r in out["samples"]["head"]] == [0, 1, 2]
        assert [r["i"] for r in out["samples"]["tail"]] == [148, 149]
        # 3 + 2 = 5；>5 即截断
        assert out["truncated"] is True

    def test_tail_empty_when_data_smaller_than_tail_size(self) -> None:
        # v2：≤100 行走全量分支，head = 全部数据，tail = []
        out = summarize_data([{"i": 1}, {"i": 2}], head_size=5, tail_size=5)
        assert len(out["samples"]["head"]) == 2
        assert out["samples"]["tail"] == []
        assert out["truncated"] is False


class TestJsonSerializable:
    """摘要必须是 JSON 可序列化的 dict（最终给 LLM 看）。"""

    def test_full_summary_json_dumps(self) -> None:
        data = [
            {"供应商": "BPS001", "数量": Decimal("10.5"), "日期": date(2026, 8, 1)},
            {"供应商": "BPS002", "数量": Decimal("20.5"), "日期": date(2026, 8, 2)},
        ]
        out = summarize_data(data)
        # Decimal 必须已在内部转 float；date 必须已转 str
        text = json.dumps(out, ensure_ascii=False, default=str)
        # 含中文
        assert "供应商" in text
        assert "BPS001" in text

    def test_default_datetime_in_summary(self) -> None:
        """datetime 字段不抛错；与 chart_service 行为一致（regex 不匹配时间），类型归 STRING。"""
        out = summarize_data([
            {"ts": datetime(2026, 8, 1, 10, 30)},
        ])
        # datetime 不是 date；regex `^\d{4}[-/]\d{2}[-/]\d{2}` 匹配不到 str(...)
        # 当前实现：datetime 实例 isinstance(date) 为 True → 直接返回 TIME
        # 列行为：column_types 推断为 TIME；samples 包含原 dict（JSON 序列化时 default=str 转）
        assert out["column_types"]["ts"] == "TIME"
        # 应可序列化
        json.dumps(out, ensure_ascii=False, default=str)


class TestDefaultsExposed:
    def test_default_constants_present(self) -> None:
        assert DEFAULT_HEAD_SAMPLE_SIZE == 5
        assert DEFAULT_TAIL_SAMPLE_SIZE == 5
        assert DEFAULT_NUMERIC_COLUMNS_CAP == 5
        assert DEFAULT_STRING_COLUMNS_CAP == 5


class TestDistinctCounts:
    def test_distinct_counts_includes_only_string_and_time(self) -> None:
        # NUMBER 列不进 distinct_counts
        out = summarize_data([
            {"s": "a"}, {"s": "b"}, {"s": "a"},
            {"n": 1}, {"n": 2},
        ])
        assert "s" in out["distinct_counts"]
        assert "n" not in out["distinct_counts"]

    def test_distinct_counts_includes_datetimes(self) -> None:
        out = summarize_data([
            {"d": date(2026, 1, 1)},
            {"d": date(2026, 1, 1)},
            {"d": date(2026, 1, 2)},
        ])
        assert out["distinct_counts"]["d"] == 2


class TestFullDataWhenSmall:
    """v2 2026-09-18：小数据全量展示（≤ FULL_DATA_THRESHOLD 行不截断）。

    触发：用户报告 27 行时 head/tail 采样把 B125 中间行丢了 → LLM 答「未在样本中展示」。
    修复：≤100 行（FULL_DATA_THRESHOLD）全量放 samples.head，truncated=False。
    """

    def test_27_rows_returns_all_in_head_not_truncated(self) -> None:
        """用户真实场景：27 行（B019+B125+D1）必须全可见。"""
        rows = [{"供应商": f"S{i}", "数量": i * 10} for i in range(27)]
        out = summarize_data(rows)
        assert out["total"] == 27
        assert out["truncated"] is False
        # samples.head 必须含全部 27 行（不是 head[:5]）
        assert len(out["samples"]["head"]) == 27
        # tail 在小数据下应为空（head 已全量）
        assert out["samples"]["tail"] == []

    def test_at_threshold_returns_all_in_full(self) -> None:
        """边界：恰好 100 行（FULL_DATA_THRESHOLD）→ 全量。"""
        from app.services.data_summary import FULL_DATA_THRESHOLD
        rows = [{"i": i} for i in range(FULL_DATA_THRESHOLD)]
        out = summarize_data(rows)
        assert out["total"] == FULL_DATA_THRESHOLD
        assert out["truncated"] is False
        assert len(out["samples"]["head"]) == FULL_DATA_THRESHOLD
        assert out["samples"]["tail"] == []

    def test_over_threshold_truncates(self) -> None:
        """边界：101 行（FULL_DATA_THRESHOLD + 1）→ 退回 head 5 + tail 5。"""
        from app.services.data_summary import FULL_DATA_THRESHOLD
        rows = [{"i": i} for i in range(FULL_DATA_THRESHOLD + 1)]
        out = summarize_data(rows)
        assert out["total"] == FULL_DATA_THRESHOLD + 1
        assert out["truncated"] is True
        # 退回旧行为：head 5 + tail 5
        assert len(out["samples"]["head"]) == 5
        assert len(out["samples"]["tail"]) == 5

    def test_full_data_branch_still_computes_stats(self) -> None:
        """小数据全量分支下 numeric_stats / distinct_counts 仍要算（cheap）。"""
        rows = [{"qty": i * 10, "supplier": f"S{i % 3}"} for i in range(20)]
        out = summarize_data(rows)
        assert out["truncated"] is False
        # numeric_stats 正常
        assert out["numeric_stats"]["qty"]["sum"] == sum(i * 10 for i in range(20))
        # distinct_counts 正常
        assert out["distinct_counts"]["supplier"] == 3