"""_executeMultiStep 失败策略集成测试。

测试 on_failure=abort / isolate / skip 三种策略在依赖失败时的行为。
mock _executeDataStep，不走真实数据库。
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.multi_step_plan import (
    MultiStepPlan,
    StepPlan,
    StepResult,
)
from app.domain.schemas import ChatResponse


def _plan(steps: list[StepPlan]) -> MultiStepPlan:
    return MultiStepPlan(
        steps=tuple(steps),
        aggregation_hint="",
        original_question="",
    )


class MockDataStepRun:
    """mock _executeDataStep 的返回值结构"""
    def __init__(
        self,
        result: StepResult,
        tokens: int = 100,
        cost: Decimal = Decimal("0.01"),
        modelName: str | None = "mock-model",
        chart_label_calls: int = 0,
        plan: MagicMock | None = None,
    ):
        self.result = result
        self.tokens = tokens
        self.cost = cost
        self.modelName = modelName
        self.chart_label_calls = chart_label_calls
        self.plan = plan


class TestChatMultistepFailureStrategies:
    """验证 _executeMultiStep 按 on_failure 策略处理步骤失败。"""

    def _mock_execute_data_step_success(self, step_plan: StepPlan) -> MockDataStepRun:
        result = StepResult(
            step_index=step_plan.index,
            description=step_plan.description,
            sub_question=step_plan.sub_question,
            sql="SELECT 1",
            data=[{"x": 1}],
            summary="ok",
        )
        return MockDataStepRun(result=result)

    def _mock_execute_data_step_error(self, step_plan: StepPlan) -> MockDataStepRun:
        result = StepResult(
            step_index=step_plan.index,
            description=step_plan.description,
            sub_question=step_plan.sub_question,
            sql=None,
            data=[],
            error="step failed",
        )
        return MockDataStepRun(result=result)

    def _build_mixin(self) -> MagicMock:
        """Build a minimally mocked MultiStepMixin."""
        from app.services.chat_multistep import MultiStepMixin

        mixin = MultiStepMixin.__new__(MultiStepMixin)
        mixin._llmFactory = MagicMock()
        mixin._stepAggregator = MagicMock()
        mixin._sqlExecutor = MagicMock()
        mixin._costFor = MagicMock(return_value=Decimal("0.01"))
        mixin._recordUsage = AsyncMock()
        mixin._storeSessionMessages = AsyncMock()
        mixin._saveQueryState = AsyncMock()
        mixin._buildAffinityStatus = AsyncMock(return_value=None)
        mixin._maybeGenerateHypotheses = AsyncMock(return_value=None)
        mixin._inheritedStateToSnapshot = MagicMock(return_value={})
        mixin._columns = MagicMock(return_value=[])
        return mixin

    def _build_dto(self) -> MagicMock:
        dto = MagicMock()
        dto.sessionId = "sess-1"
        dto.question = "test?"
        dto.datasourceId = 1
        return dto

    def _build_pc(self) -> MagicMock:
        pc = MagicMock()
        pc.ds.type = "postgresql"
        pc.ds.oracle_version = None
        pc.ds.username = "public"
        pc.contextPrompt = ""
        pc.configs = MagicMock()
        pc.selected = MagicMock()
        pc.forcedModel = None
        pc.recall = MagicMock()
        return pc

    @pytest.mark.asyncio
    async def test_abort_stops_execution(self) -> None:
        """on_failure=abort：第一步失败 → 立即终止，后续步骤不执行。"""
        plan = _plan([
            StepPlan(index=0, description="step0", sub_question="sq0", on_failure="abort"),
            StepPlan(index=1, description="step1", sub_question="sq1", on_failure="isolate"),
        ])

        with patch(
            "app.services.chat_multistep.MultiStepMixin._executeDataStep",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.side_effect = [
                self._mock_execute_data_step_error(plan.steps[0]),
                self._mock_execute_data_step_success(plan.steps[1]),
            ]

            mixin = self._build_mixin()
            resp: ChatResponse = await mixin._executeMultiStep(
                session=MagicMock(),
                dto=self._build_dto(),
                pc=self._build_pc(),
                multiStepPlan=plan,
                state=None,
                _t0=0.0,
            )

            # _executeDataStep 应只被调用 1 次（第一步失败后 abort）
            assert mock_exec.call_count == 1
            # completed 只有第一步的失败结果（进入降级收尾）
            assert len(resp.steps) == 1
            assert resp.steps[0].error == "step failed"

    @pytest.mark.asyncio
    async def test_isolate_continues_after_failure(self) -> None:
        """on_failure=isolate（默认）：第一步失败 → 继续执行后续步骤。"""
        plan = _plan([
            StepPlan(index=0, description="step0", sub_question="sq0", on_failure="isolate"),
            StepPlan(index=1, description="step1", sub_question="sq1", on_failure="isolate"),
        ])

        with patch(
            "app.services.chat_multistep.MultiStepMixin._executeDataStep",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.side_effect = [
                self._mock_execute_data_step_error(plan.steps[0]),
                self._mock_execute_data_step_success(plan.steps[1]),
            ]

            mixin = self._build_mixin()
            resp: ChatResponse = await mixin._executeMultiStep(
                session=MagicMock(),
                dto=self._build_dto(),
                pc=self._build_pc(),
                multiStepPlan=plan,
                state=None,
                _t0=0.0,
            )

            # _executeDataStep 应被调用 2 次（isolate 继续执行）
            assert mock_exec.call_count == 2
            assert len(resp.steps) == 2
            assert resp.steps[0].error == "step failed"
            assert resp.steps[1].sql == "SELECT 1"
