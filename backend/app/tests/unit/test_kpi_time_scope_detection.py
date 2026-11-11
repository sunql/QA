"""显式时间限定检测：一期只判「有/无」，不做区间解析（P4 参数化才解析）。"""

from __future__ import annotations

import pytest

from app.services.kpi_execution_service import hasExplicitTimeScope


@pytest.mark.parametrize(
    "question",
    [
        "近6个月供应商准时交付率",
        "最近 30 天的逾期率",
        "过去三个月的交付率",
        "2025年准时交付率",
        "2025-03 的交付率",
        "本季度交付率",
        "第二季度交付率",
        "Q2 交付率",
        "上半年交付率",
        "2025年上半年逾期率",
    ],
)
def test_detects_explicit_time(question: str) -> None:
    assert hasExplicitTimeScope(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "供应商准时交付率是多少",
        "KPI_SUPPLIER_OTD 这个指标",
        "逾期率为什么下降",
        "交付率",
    ],
)
def test_plain_question_has_no_time_scope(question: str) -> None:
    assert hasExplicitTimeScope(question) is False
