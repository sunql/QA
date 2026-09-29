"""供应商风险评估服务（Phase 5.4 Supplier Risk Agent）。

设计原则（plan §5.4）：
- 复用 Supplier360Service：profile / entity_mapping 查询逻辑不重复
- 主路径：RISK_SCORE（0-1，越高越优）→ 直接映射 High/Medium/Low
- Fallback：其他 3 个 feature 违规计数 → 0-1 违规 Low，2 违规 Medium，3 违规 High
- risk_points：默认 LLM 生成（自然语言 1-2 句中文）；LLM 不可用 → fallback_template
- recommended_actions：按等级静态生成（不调 LLM，响应稳定）
- ACL：与 supplier_360 一致，API 层 `getCurrentUser`，不叠加（底层 entity_mapping ACL 隔离）
- 异常隔离：LLM 调用失败 → log warn + fallback，绝不阻断主响应

Phase 6.x：`supplier_key` 接受 `str | int` —— 透传 Supplier360Service.get360。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Union

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import RiskLevel, Severity
from app.services.feature_rule_evaluator import RuleHit, _OPERATORS, _SEVERITY_ORDER
from app.services.feature_rule_registry import feature_rule_registry
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
    Supplier360Service,
)

logger = logging.getLogger(__name__)

# RISK-priority bypass: map Severity str keys (intentional — matches RuleEvaluation.matched_severity.value for legacy paths)
_SEVERITY_TO_RISK: dict[str, RiskLevel] = {
    "HIGH": RiskLevel.HIGH,
    "MEDIUM": RiskLevel.MEDIUM,
    "LOW": RiskLevel.LOW,
    "INFO": RiskLevel.LOW,
}

# LLM factory 签名：与 chatModule._llmFactory（= createClient）同形，**cfg 不是占位符**——
# 工厂需要真实 config 才能解析出 provider/api_key/model_name。传 None 只对
# 「OPENAI + 环境变量 openaiApiKey」这一种配置有意义，本项目未配置该环境变量，
# 历史实现 `llm_factory(None)` 因此恒得 None → 风险点永远走 fallback_template。
LlmFactory = Callable[[Any], Any | None]


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

    assess(session, supplier_key, *, llm_factory=None, llm_config=None) 是唯一对外入口：
    - supplier 不存在 → NotFoundError（来自 Supplier360Service，与 360° 视图同语义）
    - LLM 不可用（缺工厂 / 缺配置 / 工厂给不出客户端）→ fallback_template，
      risk_points_source=fallback_template，且**不产生任何成本数字**
    """

    async def assess(
        self,
        session: AsyncSession,
        supplier_key: Union[str, int],
        *,
        llm_factory: LlmFactory | None = None,
        llm_config: Any | None = None,
    ) -> SupplierRiskRead:
        """实时评估单供应商风险等级（plan §5.4）。

        supplier_key 同时接受 VARCHAR 业务码（如 '10105'）与 BIGINT 代理键。

        ``llm_factory`` + ``llm_config`` 必须成对提供：工厂负责构造客户端，配置提供
        单价——成本只能按实际使用模型的单价算，本服务无从自行推断。缺任一项则
        明确降级到 fallback_template（不再是「工厂拿到 None 后抛异常被吞」的隐式降级）。
        """
        view = await Supplier360Service().get360(session, supplier_key)
        contributions = [_toContribution(kpi) for kpi in view.kpis]
        level, level_source = await self._decideLevel_via_rules(view, contributions)
        actions = _buildActions(level)
        risk_points, points_source, prompt_tokens, completion_tokens, cost, model_name = await self._generateRiskPoints(
            view, contributions, level, llm_factory=llm_factory, llm_config=llm_config,
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
    # 风险等级判定（via Feature Rule Engine）
    # ------------------------------------------------------------------

    @staticmethod
    async def _decideLevel_via_rules(
        view: Supplier360Read,
        contributions: list[SupplierRiskKpiContribution],
    ) -> tuple[RiskLevel, str]:
        """4-step RISK-priority bypass wrapper (spec §6.3，与 legacy _decideLevel 字节级一致)。

        Step 1: RISK_SCORE tier matched → return immediately (bypass other violations).
        Step 2: All values missing → UNKNOWN.
        Step 3: MAX severity across other matched rules.
        Step 4: All values present, no rule matched → LOW.
        """
        # Build feature values dict from contributions
        values: dict[str, Decimal | None] = {}
        for c in contributions:
            if c.value is None:
                continue
            try:
                values[c.feature_name] = Decimal(c.value)
            except (InvalidOperation, ValueError):
                continue

        # Fetch enabled rules for SUPPLIER / FEATURE / RISK scope
        rules = feature_rule_registry.getEnabledRules(
            data_object="SUPPLIER", data_layer="FEATURE", target_level="RISK",
        )

        # Evaluate each rule: find the first (most severe) matching tier
        rule_hits: dict[str, RuleHit | None] = {}
        for rule in rules:
            value = values.get(rule.feature_name)
            if value is None:
                rule_hits[rule.code] = None
                continue
            hit: RuleHit | None = None
            for tier in sorted(rule.thresholds, key=lambda t: _SEVERITY_ORDER.get(t.severity, 99)):
                op = _OPERATORS.get(tier.operator)
                if op is None:
                    continue
                if op(value, tier.threshold_value):
                    hit = RuleHit(
                        rule_code=rule.code,
                        feature_name=rule.feature_name,
                        severity=tier.severity,
                        operator=tier.operator,
                        threshold_value=tier.threshold_value,
                        actual_value=value,
                    )
                    break
            rule_hits[rule.code] = hit

        # Step 1: RISK-priority bypass — RISK_SCORE tier matched → return immediately
        risk_score_hit = rule_hits.get("supplier_risk_score_main")
        if risk_score_hit is not None:
            return _SEVERITY_TO_RISK[risk_score_hit.severity], risk_score_hit.rule_code

        # Step 2: All missing → UNKNOWN
        all_missing = all(c.value is None for c in contributions)
        if all_missing:
            return RiskLevel.UNKNOWN, "unknown"

        # Step 3: MAX severity across other matched rules
        other_hits = [
            h for h in rule_hits.values()
            if h is not None and h.rule_code != "supplier_risk_score_main"
        ]
        if other_hits:
            worst = min(other_hits, key=lambda h: _SEVERITY_ORDER.get(h.severity, 99))
            return _SEVERITY_TO_RISK[worst.severity], worst.rule_code

        # Step 4: All values present, no rule matched → LOW
        return RiskLevel.LOW, "no_match"


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
        llm_config: Any | None,
    ) -> tuple[str | None, str, int, int, float, str | None]:
        """调 LLM 生成自然语言风险点；LLM 不可用时降级到模板。

        返回：(risk_points, source, prompt_tokens, completion_tokens, cost, model_name)

        成本单位与全系统其余路径一致：**USD**，按 ``llm_config`` 的
        ``cost_per_1k_input`` / ``cost_per_1k_output`` 计。此前硬编码「输入 0.001 /
        输出 0.002 CNY per 1k token」，与同一张 session_token_usage 台账里其他
        USD 行混在一起，成本报表与预算降级判断都把人民币当美元用。
        """
        fallback = (
            _buildFallbackRiskPoints(contributions),
            "fallback_template",
            0,
            0,
            0.0,
            None,
        )
        # 工厂与配置必须成对：只有工厂时无从定价（宁可明确降级，也不写一个来路不明的数字）
        if llm_factory is None or llm_config is None:
            return fallback

        # 客户端构造失败（无可用 API key 时 createClient 返回 None，是「无 key」的 SSOT）
        # 走同一条降级路径。历史实现把它留给下一行的 AttributeError 兜——隐式、无日志，
        # 且让「工厂传 None 占位」这个 bug 长期隐身。
        llm = llm_factory(llm_config)
        if llm is None:
            logger.warning(
                "SupplierRisk 无可用 LLM 客户端（model_config_id=%s），降级到 fallback_template",
                getattr(llm_config, "id", None),
            )
            return fallback

        try:
            messages = _buildLlmPrompt(view, contributions, level)
            response = await llm.complete(messages)
        except Exception:
            logger.warning(
                "SupplierRisk LLM 调用失败，降级到 fallback_template supplier_key=%s",
                view.profile.enterprise_key,
                exc_info=True,
            )
            return fallback

        content = getattr(response, "content", "") or ""
        # 真实 BaseLlmClient 返回 LlmResponse（camelCase: promptTokens/completionTokens/modelName）；
        # 兼容既有测试 fake（snake_case）。此前只读 snake_case 导致真实客户端计量恒为 0（审查 MEDIUM#2 根因）。
        prompt_tokens = int(getattr(response, "promptTokens", None) or getattr(response, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(response, "completionTokens", None) or getattr(response, "completion_tokens", 0) or 0)
        model_name = getattr(response, "modelName", None) or getattr(response, "model_name", None)
        # 与 chat_service._costFor / agent_runtime._cost_for_usage 同公式同口径（USD，按 config 单价）
        cost = float(
            (
                Decimal(prompt_tokens) * Decimal(str(llm_config.cost_per_1k_input))
                + Decimal(completion_tokens) * Decimal(str(llm_config.cost_per_1k_output))
            )
            / Decimal(1000)
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