"""半空计划闸门（方案A）：rowLimit/perGroupLimit 不再当「有内容」证据。

M3 已修全空计划 → 进重试。但 `{"rowLimit": 100}` 这种「只有限制无引用」的
半空计划能过 `validatePlan`（无引用可校验），模型进 SQL 生成阶段后自由选
表/编表名，最终在库侧报错被包装成「服务内部错误」。

方案 A 是最窄口径：把 `rowLimit` / `perGroupLimit` 从「有内容」证据里移除，
保留 target/selectedClasses/conditions/aggregations 等真正可查询项。
不动既有用例口径（`{"target": "查询"}` 仍合法）。
"""

from __future__ import annotations

import json

from app.domain.query_plan import QueryPlan
from app.services.nl2sql_service import Nl2SqlService, _isEmptyPlan


class TestRowLimitOnlyIsEmpty:
    def test_rowLimit_only_plan_is_empty(self) -> None:
        """仅 rowLimit 的计划视为「无可查询引用」，判失败走重试。"""
        service = Nl2SqlService()
        plan_dict = {"target": "", "rowLimit": 100}
        outcome = service._parsePlanOutcome(json.dumps(plan_dict))
        # parsePlanOutcome 应当走 PLAN_EMPTY 分支（plan is None + reason）
        assert outcome.plan is None, (
            "rowLimit-only 计划必须被判定为空（plan=None），"
            f"但解析出了 plan: {outcome.plan}"
        )
        assert outcome.reason == "PLAN_EMPTY"

    def test_perGroupLimit_only_plan_is_empty(self) -> None:
        """仅 perGroupLimit 的计划同样视为空。"""
        service = Nl2SqlService()
        plan_dict = {"target": "", "perGroupLimit": 5}
        outcome = service._parsePlanOutcome(json.dumps(plan_dict))
        assert outcome.plan is None
        assert outcome.reason == "PLAN_EMPTY"


class TestNormalPlanStillValid:
    def test_target_only_plan_is_not_empty(self) -> None:
        """target 非空的合法计划仍放行（不动既有口径）。"""
        service = Nl2SqlService()
        plan_dict = {"target": "查询采购情况"}
        outcome = service._parsePlanOutcome(json.dumps(plan_dict))
        assert outcome.plan is not None
        assert not _isEmptyPlan(outcome.plan)

    def test_selected_classes_plan_is_not_empty(self) -> None:
        """有 selectedClasses 的计划非空。"""
        service = Nl2SqlService()
        plan_dict = {"target": "", "selectedClasses": ["PRECEIPT"]}
        outcome = service._parsePlanOutcome(json.dumps(plan_dict))
        assert outcome.plan is not None
        assert not _isEmptyPlan(outcome.plan)

    def test_unanswerable_target_still_valid(self) -> None:
        """target=无法回答 是合法空计划（isUnanswerable 短路），不受 rowLimit 移除影响。"""
        service = Nl2SqlService()
        plan_dict = {"target": "无法回答"}
        outcome = service._parsePlanOutcome(json.dumps(plan_dict))
        assert outcome.plan is not None
        assert outcome.plan.isUnanswerable is True


class TestUnitGateDirectly:
    """直接对 _isEmptyPlan 做白盒断言，避免只通过 parsePlanOutcome 覆盖。"""

    def test_rowLimit_alone_makes_plan_empty(self) -> None:
        plan = QueryPlan(target="", rowLimit=100)
        assert _isEmptyPlan(plan) is True

    def test_perGroupLimit_alone_makes_plan_empty(self) -> None:
        plan = QueryPlan(target="", perGroupLimit=5)
        assert _isEmptyPlan(plan) is True