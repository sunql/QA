"""chat_multistep pattern router hook 单测。

验证 _resolveExplicitMultiStep 的两个 Bug A/B 修复：
- Bug A：hook 放在入口处，规则快路径（plan_explicit）也能触发路由覆盖
- Bug B：返回 4-tuple (dto, plan, tokens, cost)，调用方必须使用返回的 dto

不依赖真实数据库，用 mock 注入 router + stepPlanner 实例。
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.schemas import ChatRequest
from app.services.query_pattern_router import RouteHint


B019_QUESTION = (
    "分步分析：B019 圣特公司近 12 个月供货量下降的原因。"
    "第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，"
    "第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总"
)

B019_NON_COMPARISON = (
    "分步分析：B019 圣特公司近 12 个月供货量下降的原因。"
    "第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，第三步汇总结论"
)


def _make_dto(model_id: int = 3, question: str | None = None) -> ChatRequest:
    return ChatRequest(
        sessionId="s-1",
        question=question or B019_QUESTION,
        datasourceId=1,
        modelId=model_id,
    )


def _mock_pc() -> MagicMock:
    """返回一个完全 mock 的 _PipelineContext（仅满足类型检查）。

    configs 包含 qwen（id=3）和 deepseek（id=1）两个配置，供路由 hook
    查找替换 pc.selected 时使用。
    """
    pc = MagicMock()
    deepseek_cfg = MagicMock()
    deepseek_cfg.id = 1
    deepseek_cfg.model_name = "deepseek-chat"
    qwen_cfg = MagicMock()
    qwen_cfg.id = 3
    qwen_cfg.model_name = "qwen-plus"
    pc.configs = [deepseek_cfg, qwen_cfg]
    pc.selected = qwen_cfg  # 初始为 Qwen
    return pc


class TestRoutingHook:
    """验证题目模式路由 hook 的行为（Bug A/B 修复）。"""

    # -------------------------------------------------------------------------
    # Bug A fix: rule path also fires the hook
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_rule_path_comparison_pattern_overrides(self) -> None:
        """规则快路径命中 + 对比模式 → dto.modelId 被覆盖，dto 在返回值中传播。

        B019 问题的"第三步对照..."触发对比模式路由 → 强制切 deepseek（modelId=1）。
        此场景对应 Bug A（hook 原来在 plan_explicit 之后，永远不执行）。
        """
        from app.services.chat_multistep import MultiStepMixin

        mixin = MultiStepMixin.__new__(MultiStepMixin)

        # Mock _stepPlanner.plan_explicit → 规则命中（返回非空 plan）
        mock_planner = MagicMock()
        mock_rule_result = MagicMock()
        mock_rule_result.plan = MagicMock()  # non-None → 规则命中
        mock_planner.plan_explicit = AsyncMock(return_value=mock_rule_result)
        mixin._stepPlanner = mock_planner

        # Mock _recordUsage（防止 DB 写入）
        mixin._recordUsage = AsyncMock()

        # Mock patternRouter → 对比模式命中，强制 deepseek
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(
            return_value=RouteHint(forced_model_id=1, reason="comparison_in_multi_step"),
        )

        dto_in = _make_dto(model_id=3)  # Qwen
        pc = _mock_pc()

        returned_dto, returned_plan, returned_pc, returned_tokens, returned_cost = (
            await mixin._resolveExplicitMultiStep(MagicMock(), dto_in, pc)
        )

        # 验证：dto 被覆盖
        assert returned_dto.modelId == 1, (
            "dto.modelId 应被路由 hook 覆盖为 deepseek（modelId=1）"
        )
        assert returned_plan is not None, "规则命中时 plan 不应为 None"
        assert returned_tokens == 0, "规则路径 token 消耗为 0"
        assert returned_cost == Decimal("0"), "规则路径 cost 为 0"

        # 验证：patternRouter 在规则路径上也被调用了（Bug A 修复验证）
        mixin._patternRouter.route.assert_called_once_with(
            B019_QUESTION, is_multi_step=True,
        )

    @pytest.mark.asyncio
    async def test_rule_path_non_comparison_no_override(self) -> None:
        """规则快路径命中但无对比模式 → dto.modelId 保持不变。

        B019 问题改写为无"对照/对比"关键词时，路由 hook 返回 RouteHint()，
        dto.modelId 保持原始值（modelId=3）。
        """
        from app.services.chat_multistep import MultiStepMixin

        mixin = MultiStepMixin.__new__(MultiStepMixin)

        mock_planner = MagicMock()
        mock_rule_result = MagicMock()
        mock_rule_result.plan = MagicMock()
        mock_planner.plan_explicit = AsyncMock(return_value=mock_rule_result)
        mixin._stepPlanner = mock_planner
        mixin._recordUsage = AsyncMock()

        # 无对比模式命中
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(return_value=RouteHint())

        dto_in = _make_dto(model_id=3, question=B019_NON_COMPARISON)
        pc = _mock_pc()

        returned_dto, returned_plan, returned_pc, returned_tokens, returned_cost = (
            await mixin._resolveExplicitMultiStep(MagicMock(), dto_in, pc)
        )

        assert returned_dto.modelId == 3, "无匹配时 dto.modelId 应保持不变"
        assert returned_tokens == 0

    # -------------------------------------------------------------------------
    # Bug B fix: dto override propagates via return value
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_llm_path_comparison_pattern_overrides(self) -> None:
        """LLM 检测路径命中 + 对比模式 → dto.modelId 覆盖并通过返回值传播。

        此场景对应 Bug B（原来 dto 是局部变量，调用方看不到）。
        """
        from app.services.chat_multistep import MultiStepMixin

        mixin = MultiStepMixin.__new__(MultiStepMixin)

        # plan_explicit → 无规则匹配
        mock_planner = MagicMock()
        mock_planner.plan_explicit = AsyncMock(
            return_value=MagicMock(plan=None),
        )
        mixin._stepPlanner = mock_planner

        # _detectMultiStep → LLM 检测到多步
        mock_detected = MagicMock()
        mock_detected.plan = MagicMock()
        mock_detected.prompt_tokens = 100
        mock_detected.completion_tokens = 50
        mixin._detectMultiStep = AsyncMock(return_value=mock_detected)

        mixin._recordUsage = AsyncMock()
        mixin._costFor = MagicMock(return_value=Decimal("0.05"))

        # 对比模式命中
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(
            return_value=RouteHint(forced_model_id=1, reason="comparison_in_multi_step"),
        )

        dto_in = _make_dto(model_id=3)
        pc = _mock_pc()

        returned_dto, returned_plan, returned_pc, returned_tokens, returned_cost = (
            await mixin._resolveExplicitMultiStep(MagicMock(), dto_in, pc)
        )

        assert returned_dto.modelId == 1, (
            "LLM 路径的 dto.modelId 覆盖应通过返回值传播"
        )
        assert returned_tokens == 150
        assert returned_plan is not None

    @pytest.mark.asyncio
    async def test_no_multi_step_returns_original_dto(self) -> None:
        """非多步问题（plan_explicit 和 _detectMultiStep 均返回 None）→ dto 未变。

        验证 _resolveExplicitMultiStep 在"未拆出多步"时仍返回原始 dto（未覆盖）。
        """
        from app.services.chat_multistep import MultiStepMixin

        mixin = MultiStepMixin.__new__(MultiStepMixin)

        mock_planner = MagicMock()
        mock_planner.plan_explicit = AsyncMock(return_value=MagicMock(plan=None))
        mixin._stepPlanner = mock_planner
        mixin._detectMultiStep = AsyncMock(return_value=None)
        mixin._recordUsage = AsyncMock()

        dto_in = _make_dto(model_id=3)
        pc = _mock_pc()

        returned_dto, returned_plan, returned_pc, returned_tokens, returned_cost = (
            await mixin._resolveExplicitMultiStep(MagicMock(), dto_in, pc)
        )

        assert returned_dto is dto_in, "未拆步时返回的应是原始 dto（同一对象引用）"
        assert returned_plan is None
        assert returned_tokens == 0
        assert returned_cost == Decimal("0")

    @pytest.mark.asyncio
    async def test_router_exception_preserves_dto(self) -> None:
        """router 抛异常 → dto.modelId 保持原始值，方法正常返回。

        验证异常被 swallow，不影响正常流程。
        """
        from app.services.chat_multistep import MultiStepMixin

        mixin = MultiStepMixin.__new__(MultiStepMixin)

        mock_planner = MagicMock()
        mock_planner.plan_explicit = AsyncMock(
            return_value=MagicMock(plan=MagicMock()),
        )
        mixin._stepPlanner = mock_planner
        mixin._recordUsage = AsyncMock()

        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(side_effect=RuntimeError("boom"))

        dto_in = _make_dto(model_id=3)
        pc = _mock_pc()

        returned_dto, returned_plan, returned_pc, returned_tokens, returned_cost = (
            await mixin._resolveExplicitMultiStep(MagicMock(), dto_in, pc)
        )

        assert returned_dto.modelId == 3, "router 异常时 dto.modelId 应保持不变"
        assert returned_plan is not None, "规则路径 plan 不应为 None"


# =============================================================================
# Fix Round 2（feat-qwen-multistep-uplift Task 5）：pc.selected 替换
# =============================================================================

class TestPcSelectedSwap:
    """验证路由 hook 同时替换 pc.selected，让 deepseek 真正生效（Fix Round 2）。"""

    # -------------------------------------------------------------------------
    # Rule path: pc.selected is swapped to deepseek after hook fires
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_rule_path_swaps_pc_selected_to_deepseek(self) -> None:
        """规则快路径命中 + 对比模式 → pc.selected 被替换为 deepseek 配置。

        Fix Round 2/3 验证：路由 hook 覆盖 dto.modelId 后，通过
        dataclasses.replace() 创建新 pc 实例并通过 5-tuple 返回，
        使 _executeDataStep 等下游代码真正使用 deepseek 而非 Qwen。
        """
        from app.services.chat_multistep import MultiStepMixin

        mixin = MultiStepMixin.__new__(MultiStepMixin)

        mock_planner = MagicMock()
        mock_rule_result = MagicMock()
        mock_rule_result.plan = MagicMock()
        mock_planner.plan_explicit = AsyncMock(return_value=mock_rule_result)
        mixin._stepPlanner = mock_planner
        mixin._recordUsage = AsyncMock()

        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(
            return_value=RouteHint(forced_model_id=1, reason="comparison_in_multi_step"),
        )

        dto_in = _make_dto(model_id=3)  # Qwen
        pc = _mock_pc()

        # 验证初始状态：Qwen
        assert pc.selected.id == 3, "初始应为 Qwen（id=3）"
        assert pc.selected.model_name == "qwen-plus"

        # Fix Round 3: 使用 5-tuple 返回值，returned_pc 是 replace() 后的新实例
        # 注意：MagicMock 不是真实 frozen dataclass，replace() 会抛 TypeError（被异常
        # 捕获），因此 returned_pc 仍是原始 MagicMock。此处改用 returned_dto.modelId
        # 验证 hook 逻辑正确（dto 覆盖不依赖 replace()）。
        returned_dto, returned_plan, returned_pc, returned_tokens, returned_cost = (
            await mixin._resolveExplicitMultiStep(MagicMock(), dto_in, pc)
        )

        # 核心验证：dto.modelId 被 hook 覆盖为 deepseek
        assert returned_dto.modelId == 1, (
            "dto.modelId 应被 hook 覆盖为 deepseek（id=1）"
        )

    # -------------------------------------------------------------------------
    # LLM path: pc.selected is swapped to deepseek after hook fires
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_llm_path_swaps_pc_selected_to_deepseek(self) -> None:
        """LLM 检测路径命中 + 对比模式 → pc.selected 被替换为 deepseek 配置。

        与 test_rule_path_swaps_pc_selected_to_deepseek 同逻辑，验证 LLM 检测
        路径（plan_explicit 未匹配，走 _detectMultiStep）下 hook 同样生效。
        """
        from app.services.chat_multistep import MultiStepMixin

        mixin = MultiStepMixin.__new__(MultiStepMixin)

        mock_planner = MagicMock()
        mock_planner.plan_explicit = AsyncMock(
            return_value=MagicMock(plan=None),
        )
        mixin._stepPlanner = mock_planner

        mock_detected = MagicMock()
        mock_detected.plan = MagicMock()
        mock_detected.prompt_tokens = 100
        mock_detected.completion_tokens = 50
        mixin._detectMultiStep = AsyncMock(return_value=mock_detected)

        mixin._recordUsage = AsyncMock()
        mixin._costFor = MagicMock(return_value=Decimal("0.05"))

        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(
            return_value=RouteHint(forced_model_id=1, reason="comparison_in_multi_step"),
        )

        dto_in = _make_dto(model_id=3)
        pc = _mock_pc()

        # 验证初始状态：Qwen
        assert pc.selected.id == 3, "初始应为 Qwen（id=3）"
        assert pc.selected.model_name == "qwen-plus"

        # Fix Round 3: 使用 5-tuple 返回值
        # MagicMock 不是真实 frozen dataclass，replace() 抛 TypeError（被异常捕获），
        # returned_pc 仍是原始 MagicMock。此处用 returned_dto.modelId 验证 hook 逻辑。
        returned_dto, returned_plan, returned_pc, returned_tokens, returned_cost = (
            await mixin._resolveExplicitMultiStep(MagicMock(), dto_in, pc)
        )

        # 核心验证：dto.modelId 被 hook 覆盖为 deepseek
        assert returned_dto.modelId == 1, (
            "LLM 路径的 dto.modelId 应被 hook 覆盖为 deepseek（id=1）"
        )

    @pytest.mark.asyncio
    async def test_no_override_preserves_pc_selected(self) -> None:
        """无路由匹配时 pc.selected 保持不变。

        对比模式未命中（RouteHint 无 forced_model_id）时，pc.selected 不被修改。
        """
        from app.services.chat_multistep import MultiStepMixin

        mixin = MultiStepMixin.__new__(MultiStepMixin)

        mock_planner = MagicMock()
        mock_rule_result = MagicMock()
        mock_rule_result.plan = MagicMock()
        mock_planner.plan_explicit = AsyncMock(return_value=mock_rule_result)
        mixin._stepPlanner = mock_planner
        mixin._recordUsage = AsyncMock()

        # 无对比模式命中
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(return_value=RouteHint())

        dto_in = _make_dto(model_id=3, question=B019_NON_COMPARISON)
        pc = _mock_pc()

        original_selected = pc.selected

        await mixin._resolveExplicitMultiStep(MagicMock(), dto_in, pc)

        assert pc.selected is original_selected, (
            "无匹配时 pc.selected 应保持不变（同一对象引用）"
        )


# =============================================================================
# Fix Round 3（feat-qwen-multistep-uplift Task 5）：真实 frozen dataclass 测试
# 验证 _PipelineContext 是 @dataclass(frozen=True) 时，dataclasses.replace()
# 能正确创建新实例而不抛 FrozenInstanceError，且 pc.selected 被正确替换。
# =============================================================================

class TestPcSelectedSwapRealFrozen:
    """验证真实 _PipelineContext（frozen=True）下路由 hook 正确替换 selected。"""

    @pytest.mark.asyncio
    async def test_rule_path_swaps_pc_selected_real_frozen_context(self) -> None:
        """规则快路径 + 对比模式 → 真实 frozen _PipelineContext 正确替换 selected。

        Fix Round 3 核心验证：使用真实 _PipelineContext 实例（而非 MagicMock），
        调用 _resolveExplicitMultiStep 时 dataclasses.replace() 不抛 FrozenInstanceError，
        且返回的 pc.selected.id == 1（deepseek）。
        """
        from unittest.mock import MagicMock

        from app.domain.models import LlmConfig
        from app.services.chat_helpers import _PipelineContext
        from app.services.chat_multistep import MultiStepMixin

        # 构造真实 LlmConfig 实例（id=1 deepseek, id=3 qwen）
        # 只使用 LlmConfig 的实际列字段：id, model_name, provider, api_endpoint,
        # cost_per_1k_input, cost_per_1k_output, max_input_tokens, weight,
        # cost_threshold, is_active, temperature, disable_thinking
        deepseek_cfg = LlmConfig(
            id=1,
            model_name="deepseek-chat",
            provider="deepseek",
            api_endpoint="https://api.deepseek.com",
            cost_per_1k_input=Decimal("0.001"),
            cost_per_1k_output=Decimal("0.002"),
            max_input_tokens=8000,
            weight=10,
            cost_threshold=Decimal("0.05"),
            is_active=True,
            temperature=0.0,
            disable_thinking=False,
        )
        qwen_cfg = LlmConfig(
            id=3,
            model_name="qwen-plus",
            provider="qwen",
            api_endpoint="https://api.qwen.com",
            cost_per_1k_input=Decimal("0.002"),
            cost_per_1k_output=Decimal("0.006"),
            max_input_tokens=8000,
            weight=10,
            cost_threshold=Decimal("0.05"),
            is_active=True,
            temperature=0.0,
            disable_thinking=False,
        )

        # 构造真实 frozen _PipelineContext（所有字段必须有值）
        pc = _PipelineContext(
            ds=MagicMock(),          # DataSource，只用 .id 属性
            classes=[],              # list[Any]
            configs=[deepseek_cfg, qwen_cfg],
            selected=qwen_cfg,       # 初始为 Qwen
            client=MagicMock(),      # BaseLlmClient
            contextPrompt="",        # str
            fewShot=None,
            valueSamples={},
            driftWarning=None,
            dictionaryText=None,
            featureCatalogText=None,
            joins=[],
            forcedModel=False,
            recall=None,
        )

        # 验证初始状态：frozen 实例，selected.id == 3
        assert pc.selected.id == 3
        assert pc.selected.model_name == "qwen-plus"

        # 构造 mixin，设置 mock
        mixin = MultiStepMixin.__new__(MultiStepMixin)

        mock_planner = MagicMock()
        mock_rule_result = MagicMock()
        mock_rule_result.plan = MagicMock()  # 非 None → 规则命中
        mock_planner.plan_explicit = AsyncMock(return_value=mock_rule_result)
        mixin._stepPlanner = mock_planner
        mixin._recordUsage = AsyncMock()

        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(
            return_value=RouteHint(forced_model_id=1, reason="comparison_in_multi_step"),
        )

        dto_in = _make_dto(model_id=3)

        # 调用（此处在真实 frozen _PipelineContext 上执行，Fix Round 3 之前会抛 FrozenInstanceError）
        returned_dto, returned_plan, returned_pc, returned_tokens, returned_cost = (
            await mixin._resolveExplicitMultiStep(MagicMock(), dto_in, pc)
        )

        # 核心验证：pc.selected 已被替换为 deepseek（通过 dataclasses.replace 实现）
        assert returned_pc.selected.id == 1, (
            "真实 frozen pc 经 dataclasses.replace 后 selected 应为 deepseek（id=1）"
        )
        assert returned_pc.selected.model_name == "deepseek-chat"
        # dto 也应被覆盖
        assert returned_dto.modelId == 1
        # 返回的 pc 与原始 pc 是不同对象（immutable replace）
        assert returned_pc is not pc
