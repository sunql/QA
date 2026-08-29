"""StepExecutionContext 不可变 + 注入逻辑单元测试。"""

from __future__ import annotations

import pytest

from app.domain.multi_step_plan import (
    MAX_MULTI_STEP,
    MultiStepPlan,
    StepExecutionContext,
    StepPlan,
    StepResult,
    _clip_json,
    _clip_text,
)


class TestClipHelpers:
    def test_clip_text_under_limit(self) -> None:
        text = "hello"
        assert _clip_text(text, 10) == "hello"

    def test_clip_text_over_limit(self) -> None:
        text = "hello world"
        assert _clip_text(text, 5) == "hello..."

    def test_clip_json_normal(self) -> None:
        data = [{"a": 1}, {"b": 2}]
        result = _clip_json(data, 100)
        assert "a" in result
        assert "b" in result

    def test_clip_json_empty(self) -> None:
        assert _clip_json([], 50) == "[]"

    def test_clip_json_over_limit(self) -> None:
        data = [{"key": "v"}]
        result = _clip_json(data, 5)
        # json.dumps 加 default=str 序列化后是 [{"key": "v"}]，截断到 5 字符得 "[{k..."
        assert result.endswith("...")


class TestStepPlan:
    def test_basic(self) -> None:
        p = StepPlan(index=0, description="2024 sales", sub_question="2024年的销售额", aggregation_only=False)
        assert p.index == 0
        assert p.description == "2024 sales"
        assert p.aggregation_only is False

    def test_frozen(self) -> None:
        p = StepPlan(index=0, description="test", sub_question="test", aggregation_only=False)
        with pytest.raises(Exception):  # frozen dataclass是不可变的
            p.description = "changed"  # type: ignore


class TestStepResult:
    def test_basic(self) -> None:
        r = StepResult(step_index=0, description="2024 sales", sub_question="2024年的销售额", sql="SELECT ...", data=[{"year": 2024, "amount": 100}])
        assert r.step_index == 0
        assert r.sql == "SELECT ..."
        assert r.error is None
        assert r.summary == ""

    def test_error_field(self) -> None:
        r = StepResult(step_index=0, description="test", sub_question="test", error="no data")
        assert r.error == "no data"
        assert r.data == []


class TestMultiStepPlan:
    def test_is_single_step_true(self) -> None:
        plan = MultiStepPlan(
            steps=(
                StepPlan(index=0, description="2024", sub_question="2024 sales", aggregation_only=False),
                StepPlan(index=1, description="汇总", sub_question="请汇总", aggregation_only=True),
            ),
            aggregation_hint="对比两年数据",
            original_question="对比 2024 和 2025 年销售额",
        )
        # 只有 1 个数据步骤 + 1 个汇总步骤 → 视为单步，走原流水线
        assert plan.is_single_step is True
        assert plan.has_aggregation is True

    def test_is_single_step_multiple_data_steps(self) -> None:
        # 2 个数据步骤 + 1 个汇总步骤 → 多步执行
        plan = MultiStepPlan(
            steps=(
                StepPlan(index=0, description="2024", sub_question="2024 sales", aggregation_only=False),
                StepPlan(index=1, description="2025", sub_question="2025 sales", aggregation_only=False),
                StepPlan(index=2, description="汇总", sub_question="请汇总", aggregation_only=True),
            ),
            aggregation_hint="对比",
            original_question="对比 2024 和 2025 年销售额",
        )
        assert plan.is_single_step is False
        assert plan.has_aggregation is True

    def test_data_steps(self) -> None:
        plan = MultiStepPlan(
            steps=(
                StepPlan(index=0, description="2024", sub_question="2024 sales", aggregation_only=False),
                StepPlan(index=1, description="2025", sub_question="2025 sales", aggregation_only=False),
                StepPlan(index=2, description="汇总", sub_question="请汇总", aggregation_only=True),
            ),
            aggregation_hint="对比",
            original_question="对比 2024 和 2025 年",
        )
        assert len(plan.data_steps) == 2


class TestStepExecutionContext:
    def test_inject_to_prompt_index_0(self) -> None:
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
        )
        assert ctx.inject_to_prompt(0) == ""

    def test_inject_to_prompt_no_completed_steps(self) -> None:
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
            completed_steps=(),
        )
        assert ctx.inject_to_prompt(1) == ""

    def test_inject_to_prompt_contains_data(self) -> None:
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
            completed_steps=(
                StepResult(
                    step_index=0,
                    description="2024年销售额",
                    sub_question="2024年销售额是多少",
                    sql="SELECT year, SUM(amount) FROM sales WHERE year=2024",
                    data=[{"year": 2024, "amount": Decimal("1000")}],
                    summary="2024年销售额1000万",
                ),
            ),
            injection_char_limit=600,
        )
        result = ctx.inject_to_prompt(1)
        assert "2024年销售额" in result
        assert "前序步骤结果" in result

    def test_inject_to_prompt_skips_future_steps(self) -> None:
        # step_index=2 调用 inject_to_prompt(2) 时，只应看到 step_index=0 和 1
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
            completed_steps=(
                StepResult(step_index=0, description="2024", sub_question="sq", sql="S1", data=[]),
                StepResult(step_index=1, description="2025", sub_question="sq", sql="S2", data=[]),
            ),
        )
        result = ctx.inject_to_prompt(2)
        assert "2024" in result
        assert "2025" in result

    def test_with_step_returns_new_instance(self) -> None:
        ctx = StepExecutionContext(
            datasource_type="mysql",
            oracle_version=None,
            schema_prefix="test",
            context="",
        )
        result = StepResult(step_index=0, description="d", sub_question="sq")
        new_ctx = ctx.with_step(result)

        assert new_ctx is not ctx
        assert len(new_ctx.completed_steps) == 1
        assert len(ctx.completed_steps) == 0  # 原实例不受影响

    def test_frozen_immutable(self) -> None:
        ctx = StepExecutionContext(
            datasource_type="mysql",
            oracle_version=None,
            schema_prefix="test",
            context="",
        )
        with pytest.raises(Exception):
            ctx.datasource_type = "changed"  # type: ignore

    def test_max_multi_step_constant(self) -> None:
        assert MAX_MULTI_STEP == 5


from decimal import Decimal

from app.domain.multi_step_plan import (
    _ENTITY_LIST_ITEM_LIMIT,
    _MAX_ENTITY_LIST_ROWS,
    _detect_step_data_shape,
    _render_entity_list,
)


# ============================================================================
# 2026-08-17 修复：Step N 引用 Step N-1 实体列表作为 WHERE IN 筛选条件
# 渲染分档：实体列表 vs 聚合数值
# ============================================================================


class TestStepDataShapeDetection:
    """_detect_step_data_shape：步骤数据形态自动识别。"""

    def test_entity_list_top10_materials(self) -> None:
        """10 行物料 + 字符串主键列 → ENTITY_LIST。"""
        data = [{"MATERIAL_ID": f"M{i:03d}", "占比": i * 0.1} for i in range(10)]
        assert _detect_step_data_shape(data) == "ENTITY_LIST"

    def test_entity_list_with_string_col_at_50_rows(self) -> None:
        """50 行（边界）+ 字符串列 → ENTITY_LIST。"""
        data = [{"month": f"2024-{m:02d}", "amount": m * 100} for m in range(1, 51)]
        assert _detect_step_data_shape(data) == "ENTITY_LIST"

    def test_aggregate_over_50_rows(self) -> None:
        """>50 行（即使有字符串列）→ AGGREGATE（防 prompt 爆炸）。"""
        data = [{"id": f"M{i:03d}", "qty": i} for i in range(51)]
        assert _detect_step_data_shape(data) == "AGGREGATE"

    def test_aggregate_single_row_all_numeric(self) -> None:
        """单行全数值列（无字符串列）→ AGGREGATE。"""
        data = [{"订单数": 3317, "总数量": Decimal("90793450.025")}]
        assert _detect_step_data_shape(data) == "AGGREGATE"

    def test_aggregate_empty_data(self) -> None:
        """空数据 → AGGREGATE（避免下游特殊处理）。"""
        assert _detect_step_data_shape([]) == "AGGREGATE"

    def test_entity_list_with_none_string_col(self) -> None:
        """None 视为非字符串列；只 1 行全 None → 仍 AGGREGATE。"""
        data = [{"id": None, "qty": 10}]
        assert _detect_step_data_shape(data) == "AGGREGATE"

    def test_entity_list_mixed_string_and_numeric(self) -> None:
        """混合列（字符串 + 数值）→ ENTITY_LIST。"""
        data = [{"customer_id": "C001", "name": "Alice", "amount": 1000}]
        assert _detect_step_data_shape(data) == "ENTITY_LIST"

    def test_aggregate_50_rows_all_numeric(self) -> None:
        """50 行全数值（无字符串列）→ AGGREGATE。"""
        data = [{"year": 2000 + i, "sales": i * 1000.0} for i in range(50)]
        assert _detect_step_data_shape(data) == "AGGREGATE"


class TestRenderEntityList:
    """_render_entity_list：实体列表渲染。"""

    def test_single_column(self) -> None:
        """单列渲染：`列名: 值1, 值2, ...`"""
        data = [{"MATERIAL_ID": "M001"}, {"MATERIAL_ID": "M002"}]
        result = _render_entity_list(data, char_limit=1000)
        assert "MATERIAL_ID: M001, M002" in result

    def test_multi_column(self) -> None:
        """多列分别列值（用户场景：Top 10 物料 ID + 占比）。"""
        data = [
            {"MATERIAL_ID": "M001", "占比": 0.18},
            {"MATERIAL_ID": "M002", "占比": 0.15},
        ]
        result = _render_entity_list(data, char_limit=1000)
        assert "MATERIAL_ID: M001, M002" in result
        assert "占比: 0.18, 0.15" in result
        # 两列应分行（不在同一行）
        assert "MATERIAL_ID" in result and "占比" in result

    def test_empty_data(self) -> None:
        """空数据返回「（无数据）」占位。"""
        assert _render_entity_list([], char_limit=100) == "（无数据）"

    def test_over_limit_truncates_with_ellipsis(self) -> None:
        """超 char_limit 截断并加省略号。"""
        data = [{"id": f"M{i:05d}"} for i in range(100)]
        result = _render_entity_list(data, char_limit=50)
        assert len(result) <= 53  # 50 + 3 ("...")
        assert result.endswith("...")

    def test_under_limit_unchanged(self) -> None:
        """未超 char_limit 完整输出，无省略号。"""
        data = [{"id": "M001"}, {"id": "M002"}]
        result = _render_entity_list(data, char_limit=1000)
        assert "M001" in result
        assert "M002" in result
        assert not result.endswith("...")


class TestInjectToPromptEntityListRendering:
    """inject_to_prompt：按数据形态选渲染模板。"""

    def _make_top10_result(self) -> StepResult:
        return StepResult(
            step_index=1,
            description="Top 10 物料占比",
            sub_question="统计3月份主要top10采购物料的占比",
            sql="SELECT MATERIAL_ID, SUM(QTY) AS QTY FROM ... GROUP BY MATERIAL_ID ORDER BY QTY DESC FETCH FIRST 10 ROWS ONLY",
            data=[{"MATERIAL_ID": f"M{i:03d}", "占比": round(1.0 - i * 0.05, 2)} for i in range(10)],
            summary="Top 10 物料采购量占比 0.85",
        )

    def test_entity_list_tag_used(self) -> None:
        """Top 10 物料 → 渲染含 `<entity_list>` 标签。"""
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
            completed_steps=(self._make_top10_result(),),
        )
        result = ctx.inject_to_prompt(2)
        assert "[entity_list]" in result
        assert "[/entity_list]" in result
        # 完整 10 个 ID 都在
        for i in range(10):
            assert f"M{i:03d}" in result, f"应包含 M{i:03d}"

    def test_entity_list_full_id_list_no_truncation(self) -> None:
        """实体列表模式：data 截断上限默认 2000 字符，Top 10 完整可见。"""
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
            completed_steps=(self._make_top10_result(),),
        )
        result = ctx.inject_to_prompt(2)
        # Top 10（10 行 × 约 30 char = 300 char）远小于 2000 char/项
        # 验证最后一个 ID 也在渲染里
        assert "M009" in result

    def test_aggregate_tag_used_for_single_row(self) -> None:
        """单行合计（无字符串列）→ 渲染含 `<aggregate>` 标签。"""
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
            completed_steps=(
                StepResult(
                    step_index=0,
                    description="3 月采购订单数量",
                    sub_question="统计3月份采购订单数量",
                    sql="SELECT COUNT(*) AS CNT, SUM(QTY) AS QTY FROM PORDER",
                    data=[{"订单数": 3317, "总数量": Decimal("90793450.025")}],
                    summary="3 月订单数 3317，总数量 9079 万",
                ),
            ),
        )
        result = ctx.inject_to_prompt(1)
        assert "[aggregate]" in result
        assert "[/aggregate]" in result
        # 数值在 JSON 里
        assert "3317" in result

    def test_aggregate_tag_used_for_over_50_rows(self) -> None:
        """>50 行即使有字符串列也走 `<aggregate>`（不出现成对 entity_list 标签）。"""
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
            completed_steps=(
                StepResult(
                    step_index=0,
                    description="大列表",
                    sub_question="列出全部",
                    data=[{"id": f"R{i:05d}", "val": i} for i in range(60)],
                ),
            ),
        )
        result = ctx.inject_to_prompt(1)
        assert "[aggregate]" in result
        # 不应出现成对的 entity_list 标签（header 提到名称不算）
        assert "[entity_list]\n" not in result

    def test_header_text_updated(self) -> None:
        """注入产物头部文案更新为「可作为筛选条件使用」。"""
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
            completed_steps=(self._make_top10_result(),),
        )
        result = ctx.inject_to_prompt(2)
        # 头部应提示这是可作为筛选条件的数据
        assert "可作为" in result and "筛选条件" in result
        # 不再使用旧的"仅作参考数据"
        assert "仅作参考" not in result


class TestStepExecutionContextDefaults:
    """StepExecutionContext 字段默认值 + 向后兼容。"""

    def test_default_entity_list_limit_is_2000(self) -> None:
        """默认实体列表字符上限 2000（够 Top 30 完整 ID 列表）。"""
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
        )
        assert ctx.injection_char_limit_entity == _ENTITY_LIST_ITEM_LIMIT
        assert _ENTITY_LIST_ITEM_LIMIT == 2000

    def test_default_aggregate_limit_preserved(self) -> None:
        """默认聚合字符上限 600（保持向后兼容）。"""
        ctx = StepExecutionContext(
            datasource_type="postgresql",
            oracle_version=None,
            schema_prefix="public",
            context="",
        )
        assert ctx.injection_char_limit == 600

    def test_max_entity_list_rows_constant(self) -> None:
        """_MAX_ENTITY_LIST_ROWS=50 守约（>50 行强制 AGGREGATE）。"""
        assert _MAX_ENTITY_LIST_ROWS == 50

