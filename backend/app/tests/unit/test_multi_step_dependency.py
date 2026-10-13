"""MultiStepDependencyResolver 单元测试（RED: MultiStepDependencyResolver 不存在）。"""

from __future__ import annotations

import pytest

from app.domain.multi_step_plan import (
    MultiStepPlan,
    StepPlan,
    StepResult,
)
from app.services.multi_step_dependency import (
    DependencyError,
    MultiStepDependencyResolver,
)


def _plan(steps: list[StepPlan]) -> MultiStepPlan:
    return MultiStepPlan(
        steps=tuple(steps),
        aggregation_hint="",
        original_question="",
    )


class TestDependencyResolverValidate:
    def test_validate_empty_plan_passes(self) -> None:
        plan = _plan([])
        MultiStepDependencyResolver.validate(plan)  # 不 raise

    def test_validate_single_step_no_dep_passes(self) -> None:
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a"),
        ])
        MultiStepDependencyResolver.validate(plan)  # 不 raise

    def test_validate_unknown_dep_raises(self) -> None:
        """depends_on 引用不存在的 index → DependencyError"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a", depends_on=(5,)),
        ])
        with pytest.raises(DependencyError, match="unknown dep"):
            MultiStepDependencyResolver.validate(plan)

    def test_validate_out_of_range_dep_raises(self) -> None:
        """depends_on 引用 >= len(steps) 的 index → DependencyError"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a", depends_on=(1,)),
        ])
        with pytest.raises(DependencyError, match="unknown dep"):
            MultiStepDependencyResolver.validate(plan)

    def test_validate_cycle_raises(self) -> None:
        """A depends_on B, B depends_on A → DependencyError"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a", depends_on=(1,)),
            StepPlan(index=1, description="b", sub_question="b", depends_on=(0,)),
        ])
        with pytest.raises(DependencyError, match="cycle"):
            MultiStepDependencyResolver.validate(plan)

    def test_validate_self_cycle_raises(self) -> None:
        """自己依赖自己 → DependencyError"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a", depends_on=(0,)),
        ])
        with pytest.raises(DependencyError, match="cycle"):
            MultiStepDependencyResolver.validate(plan)

    def test_validate_linear_chain_passes(self) -> None:
        """0 → 1 → 2 线性链 → 通过"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a"),
            StepPlan(index=1, description="b", sub_question="b", depends_on=(0,)),
            StepPlan(index=2, description="c", sub_question="c", depends_on=(1,)),
        ])
        MultiStepDependencyResolver.validate(plan)  # 不 raise


class TestDependencyResolverTopologicalGroups:
    def test_topological_groups_empty(self) -> None:
        plan = _plan([])
        groups = MultiStepDependencyResolver.topological_groups(plan)
        assert groups == []

    def test_topological_groups_linear_chain(self) -> None:
        """0 → 1 → 2 线性链：每组单步骤，顺序 0 → 1 → 2"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a"),
            StepPlan(index=1, description="b", sub_question="b", depends_on=(0,)),
            StepPlan(index=2, description="c", sub_question="c", depends_on=(1,)),
        ])
        groups = MultiStepDependencyResolver.topological_groups(plan)
        assert [g[0] for g in groups] == [0, 1, 2]
        assert len(groups) == 3  # 每组单步骤（L1 串行）

    def test_topological_groups_no_deps(self) -> None:
        """无依赖的三步骤：按原始顺序返回"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a"),
            StepPlan(index=1, description="b", sub_question="b"),
            StepPlan(index=2, description="c", sub_question="c"),
        ])
        groups = MultiStepDependencyResolver.topological_groups(plan)
        assert [g[0] for g in groups] == [0, 1, 2]

    def test_topological_groups_fan_out(self) -> None:
        """0 无依赖；1 和 2 都依赖 0：返回 [0], [1], [2]"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a"),
            StepPlan(index=1, description="b", sub_question="b", depends_on=(0,)),
            StepPlan(index=2, description="c", sub_question="c", depends_on=(0,)),
        ])
        groups = MultiStepDependencyResolver.topological_groups(plan)
        assert [g[0] for g in groups] == [0, 1, 2]


class TestDependencyResolverApplyFailure:
    def test_apply_failure_marks_direct_downstream_skipped(self) -> None:
        """Step 0 失败，Step 1 depends_on=0 → Step 1 标 skipped"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a"),
            StepPlan(index=1, description="b", sub_question="b", depends_on=(0,)),
        ])
        results = [
            StepResult(step_index=0, description="a", sub_question="a", error="boom"),
        ]
        new_results = MultiStepDependencyResolver.apply_failure(plan, results)
        assert len(new_results) == 2
        assert new_results[0].skipped is False  # 失败本身不算 skip
        assert new_results[1].skipped is True
        assert "0" in (new_results[1].skip_reason or "")

    def test_apply_failure_does_not_mark_independent_step(self) -> None:
        """Step 0 失败，Step 1 无依赖 → Step 1 保持原样（未执行）"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a"),
            StepPlan(index=1, description="b", sub_question="b"),  # 无依赖
        ])
        results = [
            StepResult(step_index=0, description="a", sub_question="a", error="boom"),
        ]
        new_results = MultiStepDependencyResolver.apply_failure(plan, results)
        assert len(new_results) == 2
        # Step 1 从未执行过，不在 results 里，不应被标 skipped
        assert new_results[1].skipped is False

    def test_apply_failure_transitive_skip(self) -> None:
        """0 → 1 → 2，0 失败 → 1 skipped → 2 也被 skip"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a"),
            StepPlan(index=1, description="b", sub_question="b", depends_on=(0,)),
            StepPlan(index=2, description="c", sub_question="c", depends_on=(1,)),
        ])
        results = [
            StepResult(step_index=0, description="a", sub_question="a", error="boom"),
            StepResult(step_index=1, description="b", sub_question="b", skipped=True, skip_reason="dep 0 failed"),
        ]
        new_results = MultiStepDependencyResolver.apply_failure(plan, results)
        assert len(new_results) == 3
        assert new_results[2].skipped is True
        assert "1" in (new_results[2].skip_reason or "")

    def test_apply_failure_empty_plan(self) -> None:
        plan = _plan([])
        new_results = MultiStepDependencyResolver.apply_failure(plan, [])
        assert new_results == ()

    def test_apply_failure_returns_new_tuple(self) -> None:
        """apply_failure 返回新元组，不修改原 results"""
        plan = _plan([
            StepPlan(index=0, description="a", sub_question="a"),
            StepPlan(index=1, description="b", sub_question="b", depends_on=(0,)),
        ])
        original_results = [
            StepResult(step_index=0, description="a", sub_question="a", error="boom"),
        ]
        new_results = MultiStepDependencyResolver.apply_failure(plan, original_results)
        assert new_results is not original_results
        assert new_results != original_results
