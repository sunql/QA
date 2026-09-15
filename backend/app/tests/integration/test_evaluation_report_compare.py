"""评估报告对比 service 测试（feat-dq-evaluation-report，Phase 8a）。

纯函数 ``EvaluationReportCompareService.compare`` 输入两份 snapshot，
返回 ``EvaluationReportCompareRead``。覆盖：
- 相同 snapshot → delta 全 0
- overall 改善 / 退化
- 维度 + 单规则状态分类（IMPROVED / REGRESSED / UNCHANGED / NEW / REMOVED）
"""

from __future__ import annotations

import pytest

from app.services.evaluation_report_compare_service import (
    EvaluationReportCompareService,
)


def _snapshot(overall: float, dimensions: dict, rules: list) -> dict:
    return {
        "schema_version": 1,
        "overall": {"score": overall, "status": "PASS" if overall >= 80 else "FAIL"},
        "dimensions": dimensions,
        "tables": [
            {
                "target_table": "SUPPLIER",
                "rules": rules,
            }
        ],
    }


@pytest.mark.asyncio
class TestCompareService:
    async def test_identical_snapshots_delta_zero(self) -> None:
        snap = _snapshot(
            overall=85.0,
            dimensions={"completeness": 90.0, "validity": 80.0},
            rules=[
                {"rule_id": 1, "rule_code": "R1", "pass_rate": 95.0},
                {"rule_id": 2, "rule_code": "R2", "pass_rate": 75.0},
            ],
        )
        out = EvaluationReportCompareService().compare_by_ids(
            left_id=1, right_id=2, left_snapshot=snap, right_snapshot=snap,
        )
        assert out.left_id == 1
        assert out.right_id == 2
        assert out.overall_left == 85.0
        assert out.overall_right == 85.0
        assert out.overall_delta == 0.0
        # 已填的维度 delta 为 0；缺失维度 left/right 都 None → delta 也是 None
        assert all(
            d.delta in (None, 0.0) for d in out.dimension_deltas
        )
        # 已填维度（completeness/validity）的 delta 必须是 0.0
        explicit = {d.name: d for d in out.dimension_deltas}
        assert explicit["completeness"].delta == 0.0
        assert explicit["validity"].delta == 0.0
        assert all(r.delta == 0.0 for r in out.rule_deltas)
        assert all(r.status_change == "UNCHANGED" for r in out.rule_deltas)
        assert out.rules_in_left_only == []
        assert out.rules_in_right_only == []

    async def test_overall_improvement(self) -> None:
        left = _snapshot(80.0, {}, [])
        right = _snapshot(90.0, {}, [])
        out = EvaluationReportCompareService().compare_by_ids(
            1, 2, left, right,
        )
        assert out.overall_delta == pytest.approx(10.0)

    async def test_overall_regression(self) -> None:
        left = _snapshot(95.0, {}, [])
        right = _snapshot(80.0, {}, [])
        out = EvaluationReportCompareService().compare_by_ids(
            1, 2, left, right,
        )
        assert out.overall_delta == pytest.approx(-15.0)

    async def test_dimension_deltas(self) -> None:
        left = _snapshot(
            80.0,
            {"completeness": 90.0, "validity": 80.0, "uniqueness": 70.0},
            [],
        )
        right = _snapshot(
            85.0,
            {"completeness": 95.0, "validity": 75.0, "uniqueness": 80.0},
            [],
        )
        out = EvaluationReportCompareService().compare_by_ids(1, 2, left, right)
        by_name = {d.name: d for d in out.dimension_deltas}
        assert by_name["completeness"].delta == pytest.approx(5.0)
        assert by_name["validity"].delta == pytest.approx(-5.0)
        assert by_name["uniqueness"].delta == pytest.approx(10.0)

    async def test_rule_status_classification(self) -> None:
        left = _snapshot(
            0,
            {},
            rules=[
                {"rule_id": 1, "rule_code": "R1", "pass_rate": 90.0},
                {"rule_id": 2, "rule_code": "R2", "pass_rate": 95.0},
                {"rule_id": 3, "rule_code": "R3", "pass_rate": 80.0},
                {"rule_id": 4, "rule_code": "R4", "pass_rate": 50.0},
            ],
        )
        right = _snapshot(
            0,
            {},
            rules=[
                # 1：80 → 95  改善
                {"rule_id": 1, "rule_code": "R1", "pass_rate": 95.0},
                # 2：95 → 94  不变（|delta|=1 不超阈）
                {"rule_id": 2, "rule_code": "R2", "pass_rate": 94.0},
                # 3：80 → 60  退化
                {"rule_id": 3, "rule_code": "R3", "pass_rate": 60.0},
                # 4 不存在 → REMOVED
                # 5 NEW：只在 right
                {"rule_id": 5, "rule_code": "R5", "pass_rate": 70.0},
            ],
        )
        out = EvaluationReportCompareService().compare_by_ids(1, 2, left, right)
        by_rule = {r.rule_id: r for r in out.rule_deltas}
        assert by_rule[1].status_change == "IMPROVED"
        assert by_rule[2].status_change == "UNCHANGED"
        assert by_rule[3].status_change == "REGRESSED"
        assert by_rule[4].status_change == "REMOVED"
        assert by_rule[5].status_change == "NEW"

        assert out.rules_in_left_only == [4]
        assert out.rules_in_right_only == [5]

    async def test_missing_overall_score_returns_none(self) -> None:
        # snapshot 缺 overall / dimensions / rules
        left = {"tables": []}
        right = {"overall": {"score": 80.0}, "dimensions": {}, "tables": []}
        out = EvaluationReportCompareService().compare_by_ids(1, 2, left, right)
        assert out.overall_left is None
        assert out.overall_right == 80.0
        assert out.overall_delta is None
        assert out.rule_deltas == []