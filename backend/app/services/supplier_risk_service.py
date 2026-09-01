"""供应商风险评估服务（Phase 5.4 Supplier Risk Agent）。

设计原则（plan §5.4）：
- 复用 Supplier360Service：profile / entity_mapping 查询逻辑不重复
- 主路径：RISK_SCORE（0-1，越高越优）→ 直接映射 High/Medium/Low
- Fallback：其他 3 个 feature 违规计数 → 0-1 违规 Low，2 违规 Medium，3 违规 High
- risk_points：默认 LLM 生成（自然语言 1-2 句中文）；LLM 不可用 → fallback_template
- recommended_actions：按等级静态生成（不调 LLM，响应稳定）
- ACL：与 supplier_360 一致，API 层 `getCurrentUser`，不叠加（底层 entity_mapping ACL 隔离）
- 异常隔离：LLM 调用失败 → log warn + fallback，绝不阻断主响应
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import RiskLevel
from app.domain.schemas import (
    Supplier360Kpi,
    Supplier360Read,
    SupplierRiskKpiContribution,
    SupplierRiskRead,
)
from app.services.messages_zh import (
    MSG_RISK_ACTIONS_HIGH,
    MSG_RISK_ACTIONS_LOW,
    MSG_RISK_ACTIONS_MEDIUM,
    MSG_RISK_ACTIONS_UNKNOWN,
    MSG_RISK_POINTS_TEMPLATE,
)
from app.services.supplier_360_service import (
    DEFAULT_SUPPLIER_FEATURES,
    Supplier360Service,
)

logger = logging.getLogger(__name__)

# LLM factory 签名：与 chatModule._llmFactory 兼容（cfg 参数被忽略，便于复用）
LlmFactory = Callable[[Any], Any]


class _Rule:
    """单个 feature 的风险规则定义。

    operator: "lt" / "gt" / "lt_inverse"
      - lt: 实际值 < 阈值 → 违规（如 OTD < 90 触发）
      - gt: 实际值 > 阈值 → 违规（如 DEFECT_RATE > 5 触发）
      - lt_inverse: 实际值 < 阈值 → 违规（用于 0-1 区间 RISK_SCORE，越低越差）
    threshold: str（前端展示用 + 规则比较用）
    friendly_name: 中文别名（LLM prompt + 前端展示）
    """

    __slots__ = ("operator", "threshold", "friendly_name")

    def __init__(self, operator: str, threshold: str, friendly_name: str) -> None:
        self.operator = operator
        self.threshold = threshold
        self.friendly_name = friendly_name

    def violates(self, value: float) -> bool:
        threshold = float(self.threshold)
        if self.operator == "lt":
            return value < threshold
        if self.operator == "gt":
            return value > threshold
        if self.operator == "lt_inverse":
            return value < threshold
        raise ValueError(f"unknown operator: {self.operator}")


# 4 个默认 feature 的风险规则（feature_name → Rule）
RISK_RULES: dict[str, _Rule] = {
    "SUPPLIER_RISK_SCORE": _Rule(
        operator="lt_inverse", threshold="0.80", friendly_name="综合风险评分"
    ),
    "SUPPLIER_OTD_3M": _Rule(
        operator="lt", threshold="90", friendly_name="准时交付率"
    ),
    "SUPPLIER_DEFECT_RATE_3M": _Rule(
        operator="gt", threshold="5", friendly_name="缺陷率"
    ),
    "SUPPLIER_PRICE_VARIANCE_3M": _Rule(
        operator="gt", threshold="10", friendly_name="价格偏差率"
    ),
}


class SupplierRiskService:
    """供应商风险评估（Phase 5.4 Round 1）。

    assess(session, supplier_key, *, llm_factory=None) 是唯一对外入口：
    - supplier 不存在 → NotFoundError（来自 Supplier360Service，与 360° 视图同语义）
    - LLM 不可用 → fallback_template，risk_points_source=fallback_template
    """

    async def assess(
        self,
        session: AsyncSession,
        supplier_key: int,
        *,
        llm_factory: LlmFactory | None = None,
    ) -> SupplierRiskRead:
        """实时评估单供应商风险等级（plan §5.4）。"""
        view = await Supplier360Service().get360(session, supplier_key)
        contributions = [_toContribution(kpi) for kpi in view.kpis]
        level, level_source = self._decideLevel(contributions)
        actions = _buildActions(level)
        risk_points, points_source, prompt_tokens, completion_tokens, cost, model_name = await self._generateRiskPoints(
            view, contributions, level, llm_factory=llm_factory
        )
        tokens = prompt_tokens + completion_tokens
        return SupplierRiskRead(
            profile=view.profile,
            level=level,
            level_source=level_source,
            contributions=contributions,
            risk_points=risk_points,
            risk_points_source=points_source,
            recommended_actions=actions,
            tokens_used=tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            llm_model_name=model_name,
        )

    # ------------------------------------------------------------------
    # 风险等级判定
    # ------------------------------------------------------------------

    @staticmethod
    def _decideLevel(
        contributions: list[SupplierRiskKpiContribution],
    ) -> tuple[RiskLevel, str]:
        """主路径：RISK_SCORE（0-1）三档映射（<0.60 High；0.60-0.80 Medium；≥0.80 Low）。

        Fallback：其他 3 个 feature 违规计数（3→High / 2→Medium / ≤1→Low）。
        Unknown：4 个 feature 全部 missing。
        """
        risk_score = next(
            (c for c in contributions if c.feature_name == "SUPPLIER_RISK_SCORE"),
            None,
        )
        if (
            risk_score is not None
            and risk_score.value is not None
            and risk_score.value != ""
        ):
            try:
                score = float(risk_score.value)
            except (TypeError, ValueError):
                score = None
            if score is not None:
                if score < 0.60:
                    return RiskLevel.HIGH, "risk_score"
                if score < 0.80:
                    return RiskLevel.MEDIUM, "risk_score"
                return RiskLevel.LOW, "risk_score"

        # Fallback：3 个 feature 违规计数
        fallback_features = {
            "SUPPLIER_OTD_3M",
            "SUPPLIER_DEFECT_RATE_3M",
            "SUPPLIER_PRICE_VARIANCE_3M",
        }
        all_missing = all(c.value is None for c in contributions)
        if all_missing:
            return RiskLevel.UNKNOWN, "unknown"
        violations = sum(
            1
            for c in contributions
            if c.feature_name in fallback_features and c.passed is False
        )
        if violations >= 3:
            return RiskLevel.HIGH, "fallback_composite"
        if violations == 2:
            return RiskLevel.MEDIUM, "fallback_composite"
        return RiskLevel.LOW, "fallback_composite"


    # ------------------------------------------------------------------
    # LLM 生成风险点 + 降级
    # ------------------------------------------------------------------

    @staticmethod
    async def _generateRiskPoints(
        view: Supplier360Read,
        contributions: list[SupplierRiskKpiContribution],
        level: RiskLevel,
        *,
        llm_factory: LlmFactory | None,
    ) -> tuple[str | None, str, int, int, float, str | None]:
        """调 LLM 生成自然语言风险点；LLM 不可用时降级到模板。

        返回：(risk_points, source, prompt_tokens, completion_tokens, cost, model_name)
        """
        if llm_factory is None:
            return (
                _buildFallbackRiskPoints(contributions),
                "fallback_template",
                0,
                0,
                0.0,
                None,
            )

        try:
            llm = llm_factory(None)  # factory 接受 cfg 占位
            messages = _buildLlmPrompt(view, contributions, level)
            response = await llm.complete(messages)
        except Exception:
            logger.warning(
                "SupplierRisk LLM 调用失败，降级到 fallback_template supplier_key=%s",
                view.profile.enterprise_key,
                exc_info=True,
            )
            return (
                _buildFallbackRiskPoints(contributions),
                "fallback_template",
                0,
                0,
                0.0,
                None,
            )

        content = getattr(response, "content", "") or ""
        # 真实 BaseLlmClient 返回 LlmResponse（camelCase: promptTokens/completionTokens/modelName）；
        # 兼容既有测试 fake（snake_case）。此前只读 snake_case 导致真实客户端计量恒为 0（审查 MEDIUM#2 根因）。
        prompt_tokens = int(getattr(response, "promptTokens", None) or getattr(response, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(response, "completionTokens", None) or getattr(response, "completion_tokens", 0) or 0)
        model_name = getattr(response, "modelName", None) or getattr(response, "model_name", None)
        # 价格估算沿用项目惯例：输入 0.001 / 输出 0.002 CNY per 1k token（与现有 chat fakes 对齐）
        cost = round(
            (prompt_tokens / 1000.0) * 0.001
            + (completion_tokens / 1000.0) * 0.002,
            6,
        )
        return (
            content,
            "llm",
            prompt_tokens,
            completion_tokens,
            cost,
            model_name,
        )


# ---------------------------------------------------------------------------
# 模块级 helpers
# ---------------------------------------------------------------------------


def _toContribution(kpi: Supplier360Kpi) -> SupplierRiskKpiContribution:
    """把 Supplier360Kpi 转成 SupplierRiskKpiContribution。

    - value: Decimal → str（保持精度）
    - passed: 按 RISK_RULES 判定（value 为 None → passed=True 默认，不算违规）
    - note: 人类可读的「X% 低于阈值 Y%」类描述
    """
    rule = RISK_RULES.get(kpi.feature_name)
    value_str: str | None = None
    passed = True
    note: str | None = None
    threshold = rule.threshold if rule else None

    if kpi.value is not None and kpi.latest:
        # Decimal → str（保 10 位精度，前端可自己 parseFloat 截断）
        value_str = str(kpi.value)
        try:
            numeric = float(value_str)
        except (TypeError, ValueError):
            return SupplierRiskKpiContribution(
                feature_name=kpi.feature_name,
                feature_alias=kpi.feature_alias,
                value=value_str,
                unit=kpi.unit,
                threshold=threshold,
                passed=True,
                note="value 不可解析",
            )
        if rule is not None:
            passed = not rule.violates(numeric)
            unit = kpi.unit or ""
            if not passed:
                note = f"{rule.friendly_name}{numeric:g}{unit} 触发阈值 {threshold}{unit}"
            else:
                note = f"{rule.friendly_name}{numeric:g}{unit} 正常"

    return SupplierRiskKpiContribution(
        feature_name=kpi.feature_name,
        feature_alias=kpi.feature_alias,
        value=value_str,
        unit=kpi.unit,
        threshold=threshold,
        passed=passed,
        note=note,
    )


def _buildActions(level: RiskLevel) -> list[str]:
    """按风险等级返回静态建议动作（不调 LLM，响应稳定）。"""
    if level == RiskLevel.HIGH:
        return list(MSG_RISK_ACTIONS_HIGH)
    if level == RiskLevel.MEDIUM:
        return list(MSG_RISK_ACTIONS_MEDIUM)
    if level == RiskLevel.LOW:
        return list(MSG_RISK_ACTIONS_LOW)
    return list(MSG_RISK_ACTIONS_UNKNOWN)


def _buildFallbackRiskPoints(
    contributions: list[SupplierRiskKpiContribution],
) -> str:
    """LLM 不可用时的降级模板：按违规 feature 名拼接。"""
    violations = [c for c in contributions if not c.passed]
    if not violations:
        return "该供应商 4 项核心指标均正常。"
    reasons = "；".join(c.note or c.feature_name for c in violations)
    return MSG_RISK_POINTS_TEMPLATE.format(reasons=reasons)


def _buildLlmPrompt(
    view: Supplier360Read,
    contributions: list[SupplierRiskKpiContribution],
    level: RiskLevel,
) -> list[Any]:
    """构造 LLM 调用 messages：system + user。

    user content 仅含 enterprise_code + 4 个 feature 数值（不拼原 user question），
    降低 prompt injection 风险。
    """
    feature_lines = []
    for c in contributions:
        if c.value is None:
            feature_lines.append(f"- {c.feature_name}: 暂无数据")
            continue
        threshold = c.threshold or "-"
        unit = c.unit or ""
        passed = "通过" if c.passed else "违规"
        feature_lines.append(
            f"- {c.feature_name}: {c.value}{unit}（阈值 {threshold}{unit}，{passed}）"
        )
    feature_block = "\n".join(feature_lines) if feature_lines else "（无数据）"
    system = (
        "你是采购域风险评估助理。基于供应商的特征数据，输出 1-2 句中文风险点描述。"
        "严格基于事实，不臆测；如数据不足直接说明。不要列建议动作。"
    )
    user = (
        f"供应商：{view.profile.enterprise_code}（enterprise_key={view.profile.enterprise_key}）\n"
        f"风险等级：{level.value}\n"
        f"特征数据：\n{feature_block}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def buildRiskAnswer(read: SupplierRiskRead, supplier_key: int) -> str:
    """风险评估的人类可读回答（chat 拦截路径与 Agent Tool 共用，DRY）。

    - read.recommended_actions / risk_points 可能为空 → 兜底占位。
    - risk_points 超 80 字截断，避免答案卡片过长。
    """
    first_action = read.recommended_actions[0] if read.recommended_actions else "（无建议）"
    risk_excerpt = (read.risk_points or "").strip()
    if len(risk_excerpt) > 80:
        risk_excerpt = risk_excerpt[:77] + "..."
    return (
        f"供应商 {read.profile.enterprise_code}（{supplier_key}）风险等级："
        f"**{read.level.value}**。"
        f"主要风险点：{risk_excerpt or '（暂无）'}。"
        f"建议：{first_action}。"
    )