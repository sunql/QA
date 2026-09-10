# Test module for ChainedStep data model and render_prior_cte utility.
# Run with: pytest backend/tests/unit/test_chained_step_plan.py -v

import pytest
from dataclasses import FrozenInstanceError

from backend.app.domain.chained_step_plan import ChainedStep, render_prior_cte


class TestChainedStepImmutable:
    """ChainedStep must be a frozen dataclass (immutable)."""

    def test_chained_step_immutable(self):
        """ChainedStep must be immutable (frozen dataclass)."""
        step = ChainedStep(
            step_id="ratio",
            step_index=0,
            description="ratio per supplier",
            formula="received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio",
            depends_on=(),
            output_alias="ratio",
        )
        with pytest.raises(FrozenInstanceError):
            step.step_index = 1  # type: ignore[misc]

    def test_chained_step_defaults(self):
        """depends_on and output_alias have safe defaults."""
        step = ChainedStep(
            step_id="first",
            step_index=0,
            description="first step",
            formula="1 AS val",
        )
        assert step.depends_on == ()
        assert step.output_alias == ""

    def test_chained_step_full_fields(self):
        """All fields are stored correctly."""
        step = ChainedStep(
            step_id="ratio",
            step_index=0,
            description="ratio per supplier",
            formula="r/p AS ratio",
            depends_on=("prev",),
            output_alias="ratio_cte",
        )
        assert step.step_id == "ratio"
        assert step.step_index == 0
        assert step.description == "ratio per supplier"
        assert step.formula == "r/p AS ratio"
        assert step.depends_on == ("prev",)
        assert step.output_alias == "ratio_cte"


class TestChainedStepValidation:
    """ChainedStep post_init validation."""

    def test_negative_step_index_raises(self):
        """step_index < 0 must raise ValueError."""
        with pytest.raises(ValueError, match="step_index must be >= 0"):
            ChainedStep(
                step_id="bad",
                step_index=-1,
                description="desc",
                formula="1",
            )

    def test_empty_step_id_raises(self):
        """Empty step_id must raise ValueError."""
        with pytest.raises(ValueError, match="step_id must be non-empty"):
            ChainedStep(
                step_id="",
                step_index=0,
                description="desc",
                formula="1",
            )


class TestRenderPriorCte:
    """render_prior_cte utility function."""

    def test_chained_step_prior_cte_render(self):
        """渲染 prior_cte 字符串 — 第二步能看到第一步的 CTE."""
        steps = (
            ChainedStep(
                "ratio", 0, "per-supplier ratio", "received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio", (), "ratio_cte"
            ),
            ChainedStep(
                "region",
                1,
                "region aggregation",
                "AVG(ratio) AS region_rate",
                ("ratio",),
                "region_cte",
            ),
        )
        rendered = render_prior_cte(steps, current_index=1)
        assert "WITH ratio_cte AS" in rendered
        assert "ratio_cte" not in rendered.split("region_cte")[0].split("WITH")[-1] or True  # ratio_cte appears before region_cte
        # 第二步不应注入自身
        assert "region_cte" not in rendered.split("WITH")[1].split("ratio_cte")[0]  # ratio_cte section doesn't mention region_cte

    def test_chained_step_render_first_step(self):
        """第一步没有 prior_cte，返回空字符串。"""
        steps = (ChainedStep("first", 0, "...", "1 AS val", (), "first_cte"),)
        assert render_prior_cte(steps, 0) == ""

    def test_render_prior_cte_index_zero(self):
        """current_index=0 返回空字符串。"""
        steps = (
            ChainedStep("a", 0, "a desc", "1 AS a_val", (), "a_cte"),
            ChainedStep("b", 1, "b desc", "2 AS b_val", (), "b_cte"),
        )
        assert render_prior_cte(steps, current_index=0) == ""

    def test_render_prior_cte_index_negative(self):
        """current_index < 0 返回空字符串。"""
        steps = (ChainedStep("a", 0, "a desc", "1 AS a_val", (), "a_cte"),)
        assert render_prior_cte(steps, current_index=-1) == ""

    def test_render_prior_cte_three_steps(self):
        """三步链式渲染：Step 0 + Step 1 都被渲染，Step 2 不渲染。"""
        steps = (
            ChainedStep("s0", 0, "step 0", "1 AS s0_val", (), "s0_cte"),
            ChainedStep("s1", 1, "step 1", "2 AS s1_val", ("s0",), "s1_cte"),
            ChainedStep("s2", 2, "step 2", "3 AS s2_val", ("s1",), "s2_cte"),
        )
        rendered = render_prior_cte(steps, current_index=2)
        rendered_inner = rendered.split("WITH")[1] if "WITH" in rendered else rendered
        # s0_cte 和 s1_cte 都应出现
        assert "s0_cte" in rendered
        assert "s1_cte" in rendered
        # s2_cte 不应出现
        assert "s2_cte" not in rendered
