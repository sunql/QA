"""历史回放计划卡快照构造器（feat/history-replay-plan-cards）。

背景：多步/单步消息落库只有汇总文本与（单步的）sql/chart，历史回放时
分析计划（MultiStepPlanCard）与查询计划（QueryPlanCard）全部丢失。

方案：在 ``session_message`` 落一份**回放快照**（``steps_snapshot`` JSONB），
形状对齐前端 ``MultiStepStep``（camelCase），由本模块的纯函数构造 —— 不改
multi_step_run/step 表（它们是续跑的 SSOT，不是回放的形状）。
"""
from __future__ import annotations

import pytest

from app.domain.multi_step_plan import MultiStepPlan, StepPlan, StepResult
from app.services.chat_chart_persist import _PERSIST_MAX_TABLE_ROWS
from app.services.chat_helpers import buildSingleStepSnapshot, buildStepsSnapshot


def _plan(*steps: StepPlan) -> MultiStepPlan:
    return MultiStepPlan(steps=tuple(steps), aggregation_hint="汇总对比", original_question="Q")


def _result(index: int, **kw) -> StepResult:
    return StepResult(step_index=index, description=f"步{index}", sub_question=f"子问题{index}", **kw)


class TestBuildStepsSnapshot:
    def test_data_steps_and_aggregation_step_all_present(self):
        """快照 = 数据步（来自 completed）+ 汇总步（计划里有但从不落 completed）。"""
        plan = _plan(
            StepPlan(index=0, description="A 年销量", sub_question="A 年销量是多少"),
            StepPlan(index=1, description="B 年销量", sub_question="B 年销量是多少"),
            StepPlan(index=2, description="汇总对比", sub_question="请对比两年", aggregation_only=True),
        )
        completed = [_result(0, sql="SELECT 1", summary="A=1"), _result(1, sql="SELECT 2")]
        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={}, completed=completed, runId="r-1")

        assert [s["stepIndex"] for s in snapshot] == [0, 1, 2]
        assert snapshot[2]["aggregationOnly"] is True
        assert snapshot[2]["status"] == "done"
        assert snapshot[2]["sql"] is None
        # runId 盖到每一步（前端失败步的续跑按钮靠它）
        assert all(s["runId"] == "r-1" for s in snapshot)
        # 数据步逐字保真
        assert snapshot[0]["sql"] == "SELECT 1"
        assert snapshot[0]["summary"] == "A=1"
        assert snapshot[0]["aggregationOnly"] is False

    def test_db_status_wins_over_result_for_compressed(self):
        """被上下文压缩的步：终态以 DB 行为准（compressed），StepResult 本身不知道压缩。"""
        plan = _plan(StepPlan(index=0, description="A", sub_question="A?"))
        db_row = type("Row", (), {"status": "compressed", "sql": "SELECT 1"})()
        completed = [_result(0, sql="SELECT 1")]
        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={0: db_row}, completed=completed, runId=None)
        assert snapshot[0]["status"] == "compressed"

    def test_failed_step_maps_to_error_status(self):
        plan = _plan(StepPlan(index=0, description="A", sub_question="A?"))
        completed = [_result(0, error="超时")]
        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={}, completed=completed, runId=None)
        assert snapshot[0]["status"] == "error"
        assert snapshot[0]["error"] == "超时"

    def test_db_failed_status_maps_to_error(self):
        plan = _plan(StepPlan(index=0, description="A", sub_question="A?"))
        db_row = type("Row", (), {"status": "failed", "sql": None})()
        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={0: db_row}, completed=[], runId=None)
        assert snapshot[0]["status"] == "error"

    def test_result_rows_are_excluded(self):
        """`data` 不进快照：TOP-N 全量行已由 chartOption（TABLE rows）承载，
        与顶层 chart_type/chart_option 落库口径一致（0105 起 data 不落库）。"""
        plan = _plan(StepPlan(index=0, description="A", sub_question="A?"))
        completed = [_result(0, sql="SELECT 1", data=[{"a": 1}, {"a": 2}])]
        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={}, completed=completed, runId=None)
        assert "data" not in snapshot[0]

    def test_query_plan_serialized_per_step(self):
        plan = _plan(StepPlan(index=0, description="A", sub_question="A?"))
        query_plan = type("QP", (), {"to_dict": lambda self: {"target": "销量"}})()
        completed = [_result(0, sql="SELECT 1", query_plan=query_plan)]
        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={}, completed=completed, runId=None)
        assert snapshot[0]["queryPlan"] == {"target": "销量"}

    def test_chart_payload_carried_through(self):
        plan = _plan(StepPlan(index=0, description="A", sub_question="A?"))
        completed = [_result(0, sql="SELECT 1", chart_type="bar", chart_option={"series": []})]
        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={}, completed=completed, runId=None)
        assert snapshot[0]["chartType"] == "bar"
        assert snapshot[0]["chartOption"] == {"series": []}

    def test_oversized_table_payload_is_bounded_like_top_level(self):
        """快照与顶层同一落库口径：table rows 截到 200 并置 truncated。

        顶层 ``chart_option`` 落库前过 ``boundedChartOption``，快照这条路径原先
        只做了 ``jsonSafe`` ⇒ 十万行明细会原样写进 JSONB，导出 PDF 时再全量
        灌进 reportlab。不可变：原始 chart_option 不能被就地截断（实时响应仍发全量）。
        """
        plan = _plan(StepPlan(index=0, description="A", sub_question="A?"))
        rows = [{"a": i} for i in range(_PERSIST_MAX_TABLE_ROWS + 5)]
        chartOption = {"columns": ["a"], "rows": rows}
        rendered = [_result(0, sql="SELECT 1", chart_type="table", chart_option=chartOption)]

        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={}, completed=rendered, runId=None)

        assert len(snapshot[0]["chartOption"]["rows"]) == _PERSIST_MAX_TABLE_ROWS
        assert snapshot[0]["chartOption"]["truncated"] is True
        assert len(rows) == _PERSIST_MAX_TABLE_ROWS + 5

    def test_oversized_table_option_is_bounded_too(self):
        plan = _plan(StepPlan(index=0, description="A", sub_question="A?"))
        tableOption = {"columns": ["a"], "rows": [{"a": i} for i in range(_PERSIST_MAX_TABLE_ROWS + 1)]}
        completed = [_result(0, sql="SELECT 1", table_option=tableOption)]

        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={}, completed=completed, runId=None)

        assert len(snapshot[0]["tableOption"]["rows"]) == _PERSIST_MAX_TABLE_ROWS

    def test_step_missing_everything_still_emits_pending_placeholder_shape(self):
        """run 被 kill switch 关掉时 DB 行与结果都可能缺：入口仍要给该步一个
        合法形状（前端 MultiStepStep 渲染不许拿到 undefined 崩溃）。"""
        plan = _plan(StepPlan(index=0, description="A", sub_question="A?"))
        snapshot = buildStepsSnapshot(plan=plan, stepsByIdx={}, completed=[], runId=None)
        assert snapshot[0]["status"] == "done"  # 走到汇总即视为完成，无失败证据不标错
        assert snapshot[0]["sql"] is None


class TestBuildSingleStepSnapshot:
    def test_minimal_shape_without_per_step_query_plan(self):
        """单步：查询计划随顶层 `query_plan` 列落库（对应消息级 QueryPlanCard），
        快照里**不带** per-step queryPlan —— 与线上单步不渲染步内计划卡一致。"""
        snapshot = buildSingleStepSnapshot(description="供应商", subQuestion="有多少家", sql="SELECT 1")
        assert snapshot == [
            {
                "stepIndex": 0,
                "description": "供应商",
                "subQuestion": "有多少家",
                "aggregationOnly": False,
                "status": "done",
                "sql": "SELECT 1",
                "summary": "",
                "error": None,
                "chartType": None,
                "chartOption": None,
                "tableOption": None,
                "visualRationale": None,
                "queryPlan": None,
                "runId": None,
            }
        ]

    def test_unanswerable_variant(self):
        snapshot = buildSingleStepSnapshot(
            description="无法回答", subQuestion="Q", sql=None, summary="该问题当前数据条件下无法回答",
        )
        assert snapshot[0]["sql"] is None
        assert snapshot[0]["status"] == "done"
        assert snapshot[0]["summary"] == "该问题当前数据条件下无法回答"
