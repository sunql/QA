"""KPI / 指标状态流转矩阵单测（P5 治理收口）。

矩阵是纯函数，不查库：
- KPI：DRAFT→PUBLISHED、PUBLISHED→DEPRECATED、DEPRECATED→DRAFT；同态 no-op；
  其余（含未知状态）一律 ConflictError(detail="INVALID_STATUS_TRANSITION")。
- 指标：DRAFT→PUBLISHED；PUBLISHED→DRAFT 仅在无已发布 KPI 挂靠时允许；同态 no-op；
  未知状态失败关闭。
"""

from __future__ import annotations

import pytest

from app.domain.exceptions import ConflictError
from app.services.kpi_status_machine import assertKpiTransition, assertMetricTransition

ALLOWED_KPI = [
    ("DRAFT", "PUBLISHED"),
    ("PUBLISHED", "DEPRECATED"),
    ("DEPRECATED", "DRAFT"),
    ("DRAFT", "DRAFT"),
]
FORBIDDEN_KPI = [
    ("PUBLISHED", "DRAFT"),
    ("DRAFT", "DEPRECATED"),
    ("DEPRECATED", "PUBLISHED"),
]


@pytest.mark.parametrize("src,dst", ALLOWED_KPI)
def test_kpi_allowed_transitions(src: str, dst: str) -> None:
    assertKpiTransition(src, dst)


@pytest.mark.parametrize("src,dst", FORBIDDEN_KPI)
def test_kpi_forbidden_transitions(src: str, dst: str) -> None:
    with pytest.raises(ConflictError) as exc:
        assertKpiTransition(src, dst)
    assert exc.value.detail == "INVALID_STATUS_TRANSITION"
    assert exc.value.details["from"] == src
    assert exc.value.details["to"] == dst
    # 报错要告诉调用方「当前状态能去哪」，否则前端只能猜
    assert exc.value.details["allowed"] == sorted(
        {"DRAFT": {"PUBLISHED"}, "PUBLISHED": {"DEPRECATED"}, "DEPRECATED": {"DRAFT"}}[src]
    )


def test_metric_published_to_draft_blocked_when_linked_by_published_kpi() -> None:
    with pytest.raises(ConflictError) as exc:
        assertMetricTransition("PUBLISHED", "DRAFT", publishedKpiCount=1)
    assert exc.value.detail == "INVALID_STATUS_TRANSITION"
    assert exc.value.details == {"from": "PUBLISHED", "to": "DRAFT", "allowed": []}


def test_metric_published_to_draft_ok_when_no_published_kpi() -> None:
    assertMetricTransition("PUBLISHED", "DRAFT", publishedKpiCount=0)


def test_metric_draft_to_published_ok() -> None:
    assertMetricTransition("DRAFT", "PUBLISHED", publishedKpiCount=0)


def test_metric_same_status_is_noop_even_when_linked() -> None:
    assertMetricTransition("PUBLISHED", "PUBLISHED", publishedKpiCount=3)


def test_unknown_status_fails_closed() -> None:
    with pytest.raises(ConflictError):
        assertKpiTransition("ARCHIVED", "DRAFT")
    with pytest.raises(ConflictError):
        assertKpiTransition("DRAFT", "ARCHIVED")
    with pytest.raises(ConflictError):
        assertMetricTransition("ARCHIVED", "ARCHIVED", publishedKpiCount=0)


def test_enum_members_are_normalized() -> None:
    """DB 值可能是 str，也可能是枚举成员（str Enum）；两者都要能过矩阵。"""
    from app.domain.enums import KpiStatus, MetricStatus

    assertKpiTransition(KpiStatus.DRAFT, KpiStatus.PUBLISHED)
    assertMetricTransition(MetricStatus.DRAFT, MetricStatus.PUBLISHED, publishedKpiCount=0)
