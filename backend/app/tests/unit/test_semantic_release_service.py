"""语义发布写路径（P1b / D10）的纯逻辑用例。

真实 PG 的状态迁移在 `integration/test_semantic_release_service.py`；这里钉两件
「靠集成测也看不出对错」的事：

1. **发布闸门分级**：告警与阻断的边界（`DRAFT` 指标同型问题只告警，`PUBLISHED`
   的 RATIO/DIFFERENCE 缺落点才拒绝发布），以及报告体量封顶。
2. **状态迁移矩阵**：合法迁移 / 非法迁移逐条列举；ARCHIVED 是终态。
3. **撞库翻译**：唯一索引 / 外键错误必须变成领域错误（409/422），不能漏成 500。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.exceptions import ConflictError, ValidationError
from app.services import semantic_release_service as releaseServiceModule
from app.services.semantic_release_service import (
    PublishGateResult,
    evaluatePublishGate,
    isTransitionAllowed,
)


def _measure(className: str | None = "DWD_CLASS") -> dict[str, Any]:
    return {
        "agg": "SUM",
        "expr": None,
        "filter": None,
        "className": className,
        "classAlias": None,
    }


def _metric(
    *,
    name: str = "M_RATIO",
    metricType: str = "RATIO",
    status: str = "DRAFT",
    sources: list[dict[str, Any]] | None = None,
    numerator: dict[str, Any] | None | str = "default",
    denominator: dict[str, Any] | None | str = "default",
) -> dict[str, Any]:
    return {
        "metricName": name,
        "metricType": metricType,
        "status": status,
        "sources": [] if sources is None else sources,
        "numeratorMeasure": _measure() if numerator == "default" else numerator,
        "denominatorMeasure": _measure() if denominator == "default" else denominator,
    }


def _manifest(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schemaVersion": "v1", "metrics": metrics}


# ---------------------------------------------------------------------------
# 发布闸门
# ---------------------------------------------------------------------------


def test_metric_without_sources_is_a_warning_not_a_blocker() -> None:
    """无源指标是治理债，必须显式暴露，但不阻止发布（D07 第 1 条）。"""
    gate = evaluatePublishGate(_manifest([_metric(status="DRAFT")]))

    assert gate.canPublish is True
    assert gate.blockers == ()
    assert any("M_RATIO" in message and "sources" in message for message in gate.warnings)


def test_published_ratio_without_landing_is_blocked() -> None:
    """已发布的比率缺分子/分母落点 ⇒ 不可执行，拒绝发布（D07 第 2 条）。"""
    gate = evaluatePublishGate(
        _manifest(
            [
                _metric(
                    status="PUBLISHED",
                    numerator=_measure(className=None),
                    denominator=None,
                )
            ]
        )
    )

    assert gate.canPublish is False
    assert len(gate.blockers) == 2
    assert any("分子" in message for message in gate.blockers)
    assert any("分母" in message for message in gate.blockers)


def test_draft_ratio_without_landing_is_only_a_warning() -> None:
    """当前治理库 12 条指标全是 DRAFT —— 首个快照不能被闸门卡住。"""
    gate = evaluatePublishGate(
        _manifest(
            [
                _metric(
                    status="DRAFT",
                    sources=[{"className": "DWD_CLASS", "role": "BASE"}],
                    numerator=_measure(className=None),
                    denominator=None,
                )
            ]
        )
    )

    assert gate.canPublish is True
    assert gate.blockers == ()
    assert len(gate.warnings) == 2


def test_published_measure_without_landing_is_not_blocked() -> None:
    """硬拒绝只针对 RATIO/DIFFERENCE：MEASURE 的落点缺失可在运行期用公式兜底。"""
    gate = evaluatePublishGate(
        _manifest(
            [
                _metric(
                    metricType="MEASURE",
                    status="PUBLISHED",
                    numerator=_measure(className=None),
                    denominator=None,
                )
            ]
        )
    )

    assert gate.canPublish is True
    assert len(gate.warnings) == 2


def test_healthy_metric_passes_the_gate_silently() -> None:
    gate = evaluatePublishGate(
        _manifest(
            [
                _metric(
                    status="PUBLISHED",
                    sources=[{"className": "DWD_CLASS", "role": "BASE"}],
                )
            ]
        )
    )

    assert gate == PublishGateResult()


def test_gate_warning_report_is_capped() -> None:
    """异常体量的治理数据不得把写接口的响应撑爆。"""
    metrics = [_metric(name=f"M{index}") for index in range(50)]

    gate = evaluatePublishGate(_manifest(metrics))

    assert len(gate.warnings) == releaseServiceModule._MAX_GATE_MESSAGES


def test_gate_blocker_report_is_capped() -> None:
    """阻断项与告警各自封顶（两条切片必须独立生效）。"""
    metrics = [
        _metric(
            name=f"M{index}",
            status="PUBLISHED",
            numerator=_measure(className=None),
            denominator=None,
        )
        for index in range(50)
    ]

    gate = evaluatePublishGate(_manifest(metrics))

    assert gate.canPublish is False
    assert len(gate.blockers) == releaseServiceModule._MAX_GATE_MESSAGES


# ---------------------------------------------------------------------------
# 状态迁移矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fromStatus", "toStatus"),
    [
        ("DRAFT", "VALIDATED"),
        ("VALIDATED", "ACTIVE"),
        ("ACTIVE", "ARCHIVED"),
    ],
)
def test_legal_transitions(fromStatus: str, toStatus: str) -> None:
    assert isTransitionAllowed(fromStatus, toStatus) is True


@pytest.mark.parametrize(
    ("fromStatus", "toStatus"),
    [
        ("DRAFT", "ACTIVE"),
        ("DRAFT", "ARCHIVED"),
        ("VALIDATED", "ARCHIVED"),
        ("VALIDATED", "DRAFT"),
        ("ACTIVE", "VALIDATED"),
        ("ACTIVE", "ACTIVE"),
        ("ARCHIVED", "ACTIVE"),
        ("ARCHIVED", "VALIDATED"),
        ("ARCHIVED", "DRAFT"),
        ("ARCHIVED", "ARCHIVED"),
    ],
)
def test_illegal_transitions(fromStatus: str, toStatus: str) -> None:
    assert isTransitionAllowed(fromStatus, toStatus) is False


def test_unknown_status_has_no_transitions() -> None:
    """库侧 CHECK 兜底；服务层不认识的字符串不得被当成「可以走」."""
    assert isTransitionAllowed("PUBLISHED", "ACTIVE") is False


# ---------------------------------------------------------------------------
# 撞库翻译
# ---------------------------------------------------------------------------


def _integrityError(sqlstate: str) -> IntegrityError:
    orig = Exception("db error")
    orig.sqlstate = sqlstate  # type: ignore[attr-defined]
    return IntegrityError("statement", {}, orig)


def test_fk_violation_maps_to_domain_validation_error() -> None:
    mapped = releaseServiceModule._mapCreateIntegrityError(
        _integrityError("23503"), "2026.11.10-1", "PROCUREMENT"
    )

    assert isinstance(mapped, ValidationError)


def test_unique_violation_maps_to_version_conflict() -> None:
    mapped = releaseServiceModule._mapCreateIntegrityError(
        _integrityError("23505"), "2026.11.10-1", None
    )

    assert isinstance(mapped, ConflictError)
    assert "2026.11.10-1" in str(mapped)
