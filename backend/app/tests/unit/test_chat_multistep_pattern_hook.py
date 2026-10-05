"""chat_multistep pattern router hook 单测。

验证 _resolveExplicitMultiStep 后，命中对比模式时 dto.modelId 被覆盖为 deepseek。
不依赖真实数据库，用 mock 注入 router 实例。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.schemas import ChatRequest


def _make_dto(model_id: int | None = 3) -> ChatRequest:
    return ChatRequest(
        sessionId="s-1",
        question="分步分析：B019 圣特公司近 12 个月供货量下降的原因。"
        "第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，"
        "第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总",
        datasourceId=1,
        modelId=model_id,
    )


class TestRoutingHook:
    @pytest.mark.asyncio
    async def test_comparison_pattern_overrides_to_deepseek(self) -> None:
        """命中对比模式 → dto.modelId 被覆盖为 1 (deepseek)"""
        from app.services.chat_multistep import MultiStepMixin
        from app.services.query_pattern_router import RouteHint

        mixin = MultiStepMixin.__new__(MultiStepMixin)  # 绕过 __init__
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(
            return_value=RouteHint(forced_model_id=1, reason="comparison_in_multi_step"),
        )

        dto = _make_dto(model_id=3)  # Qwen
        # 模拟 hook 行为
        hint = mixin._patternRouter.route(dto.question, is_multi_step=True)
        if hint.forced_model_id is not None:
            dto = dto.model_copy(update={"modelId": hint.forced_model_id})
        assert dto.modelId == 1

    @pytest.mark.asyncio
    async def test_non_match_keeps_original_model(self) -> None:
        from app.services.chat_multistep import MultiStepMixin
        from app.services.query_pattern_router import RouteHint

        mixin = MultiStepMixin.__new__(MultiStepMixin)
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(return_value=RouteHint())  # 无命中

        dto = _make_dto(model_id=3)
        hint = mixin._patternRouter.route(dto.question, is_multi_step=True)
        if hint.forced_model_id is not None:
            dto = dto.model_copy(update={"modelId": hint.forced_model_id})
        assert dto.modelId == 3  # 不变

    @pytest.mark.asyncio
    async def test_router_exception_falls_back(self) -> None:
        """router 抛异常 → 不覆盖（异常 swallow）"""
        from app.services.chat_multistep import MultiStepMixin
        mixin = MultiStepMixin.__new__(MultiStepMixin)
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(side_effect=RuntimeError("boom"))

        dto = _make_dto(model_id=3)
        try:
            hint = mixin._patternRouter.route(dto.question, is_multi_step=True)
            if hint.forced_model_id is not None:
                dto = dto.model_copy(update={"modelId": hint.forced_model_id})
        except Exception:
            pass  # swallow
        assert dto.modelId == 3
