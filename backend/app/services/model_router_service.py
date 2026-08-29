"""模型路由服务 - 核心调度逻辑。

实现设计稿的加权随机 + 成本阈值熔断 + 会话亲和 + 累计成本降级策略。
纯函数式：selectModel 不修改入参 configs，返回选中的 LlmConfig。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.config import Settings, getSettings
from app.domain.enums import ProviderType
from app.domain.exceptions import NoAvailableModelError
from app.infrastructure.token_counter.base_counter import TokenCounter
from app.services.messages_zh import (
    MSG_NO_ENABLED_MODEL,
    MSG_NO_MODEL_AVAILABLE,
)

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET_FACTOR = Decimal(1000)


@dataclass(frozen=True)
class RoutingContext:
    """路由上下文（不可变）。"""

    sessionId: str
    sessionCost: float = 0.0
    sessionTurnCount: int = 0
    priorModelId: int | None = None


class _RngLike(Protocol):
    def random(self) -> float: ...


class ModelRouterService:
    """模型路由器。"""

    def __init__(
        self,
        *,
        tokenCounter: TokenCounter | None = None,
        settings: Settings | None = None,
        rng: _RngLike | random.Random | None = None,
        affinityTurns: int | None = None,
    ) -> None:
        self._tokenCounter = tokenCounter
        self._settings = settings or getSettings()
        self._rng = rng or random.Random()
        self._affinityTurns = (
            affinityTurns if affinityTurns is not None else self._settings.sessionAffinityTurns
        )

    def selectModel(self, configs: list[Any], prompt: str, ctx: RoutingContext) -> Any:
        """从 configs 中选择一个模型。不修改入参。"""
        if not configs:
            raise NoAvailableModelError(MSG_NO_MODEL_AVAILABLE)
        active = [c for c in configs if c.is_active]
        if not active:
            raise NoAvailableModelError(MSG_NO_ENABLED_MODEL)

        # 7. 累计成本超预算 -> 强制最便宜
        budget = Decimal(str(self._settings.sessionBudget))
        if Decimal(str(ctx.sessionCost)) >= budget:
            logger.info("会话 %s 累计成本 %.4f 超预算 %.4f，降级到最便宜模型", ctx.sessionId, ctx.sessionCost, float(budget))
            return _cheapest(active)

        # 4. 会话亲和：前 N 轮沿用上一轮模型（若仍启用）
        if ctx.sessionTurnCount < self._affinityTurns and ctx.priorModelId is not None:
            prior = next((c for c in active if c.id == ctx.priorModelId), None)
            if prior is not None:
                logger.debug("会话 %s 亲和模型 %s", ctx.sessionId, prior.model_name)
                return prior

        # 3. 过滤超过成本阈值的模型
        candidates = [c for c in active if not self._exceedsThreshold(c, prompt, ctx.sessionCost)]
        if not candidates:
            logger.info("会话 %s 全部模型超阈值，降级到最便宜模型", ctx.sessionId)
            return _cheapest(active)

        # 5. 加权随机
        return _weightedChoice(candidates, self._rng)

    def selectFallbackModel(self, configs: list[Any], excludeId: int | None) -> Any | None:
        """选出可用的最便宜备选模型（排除指定 id），无可选时返回 None。不修改入参。

        用于 LLM 调用失败后的降级重试：仅考虑启用的模型，且避开刚失败的主模型。
        """
        candidates = [c for c in configs if c.is_active and (excludeId is None or c.id != excludeId)]
        if not candidates:
            return None
        return _cheapest(candidates)

    def _exceedsThreshold(self, config: Any, prompt: str, sessionCost: float) -> bool:
        """估算本次 prompt 成本，判断是否超过该模型的成本阈值。"""
        tokens = self._countTokens(config, prompt)
        estPromptCost = Decimal(tokens) * config.cost_per_1k_input / _DEFAULT_BUDGET_FACTOR
        totalEst = Decimal(str(sessionCost)) + estPromptCost
        return totalEst >= config.cost_threshold

    def _countTokens(self, config: Any, prompt: str) -> int:
        if self._tokenCounter is not None:
            return self._tokenCounter.countTokens(config.model_name, prompt)
        # 懒导入避免循环依赖
        from app.services.token_counter_provider import providerTokenCounter

        provider = ProviderType(config.provider)
        return providerTokenCounter(provider).countTokens(config.model_name, prompt)

    def estimatePromptCost(self, config: Any, prompt: str) -> Decimal:
        """估算某模型处理 prompt 的输入成本（不含输出）。"""
        tokens = self._countTokens(config, prompt)
        return Decimal(tokens) * config.cost_per_1k_input / _DEFAULT_BUDGET_FACTOR


def _cheapest(configs: list[Any]) -> Any:
    """返回输入单价最低的模型。"""
    return min(configs, key=lambda c: c.cost_per_1k_input)


def _weightedChoice(candidates: list[Any], rng: _RngLike) -> Any:
    """按 weight 加权随机选择；权重 0 的模型不会被选中（当存在正权重候选时）。"""
    weights = [max(int(c.weight), 0) for c in candidates]
    total = sum(weights)
    if total == 0:
        # 所有权重为 0，等概率随机
        return _fallbackChoice(candidates, rng)
    r = rng.random() * total
    upto = 0.0
    for candidate, w in zip(candidates, weights, strict=True):
        upto += w
        if r < upto:
            return candidate
    return candidates[-1]


def _fallbackChoice(candidates: list[Any], rng: _RngLike) -> Any:
    idx = int(rng.random() * len(candidates))
    if idx >= len(candidates):
        idx = len(candidates) - 1
    return candidates[idx]
