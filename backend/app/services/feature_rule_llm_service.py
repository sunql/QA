"""parse-description LLM 服务（spec §7，config-time only，不写 DB）。

候选 feature_name 从 feature_definition 加载 → system prompt 注入 →
LLM 返回 Pydantic 结构化输出 → 校验通过返回；任意异常 → LLMUnavailableError(503)。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import FeatureStatus
from app.domain.exceptions import LLMUnavailableError
from app.domain.models import FeatureDefinition
from app.domain.schemas import (
    FeatureRuleParseDescriptionRequest,
    FeatureRuleParseDescriptionResponse,
)

logger = logging.getLogger(__name__)


async def _loadCandidateFeatures(
    session: AsyncSession, data_object: str, data_layer: str
) -> list[dict]:
    rows = (await session.execute(
        select(FeatureDefinition).where(
            FeatureDefinition.entity_type == data_object,
            FeatureDefinition.is_enabled == True,
            FeatureDefinition.status == FeatureStatus.ACTIVE,
        )
    )).scalars().all()
    return [
        {"feature_name": r.feature_name, "feature_alias": r.feature_alias, "unit": r.unit}
        for r in rows
    ]


def _buildSystemPrompt(candidates: list[dict]) -> str:
    cand_block = "\n".join(
        f"- {c['feature_name']}（{c['feature_alias'] or ''}，unit={c['unit'] or '-'}）"
        for c in candidates
    )
    return (
        "你是 Feature Rule 配置助理。基于候选 feature 列表，把管理员的自然语言策略"
        "解析为结构化阈值建议。严格只用候选 feature_name；不要臆造。"
        "\n\n候选 features:\n"
        f"{cand_block}\n\n"
        "输出 JSON：\n"
        '{"suggested_thresholds": [...], "reasoning": "...", "overall_confidence": 0.0-1.0, "warnings": [...]}\n'
        "每条 suggested_threshold 字段: feature_name, severity(HIGH/MEDIUM/LOW/INFO), "
        "operator(lt/lte/gt/gte/lt_inverse), threshold_value(number), unit, confidence, rationale"
    )


async def parseFeatureRuleDescription(
    session: AsyncSession,
    payload: FeatureRuleParseDescriptionRequest,
    llm_client: Any,
    actor: str | None = None,
) -> FeatureRuleParseDescriptionResponse:
    """调 LLM 解析自然语言；失败 → 503。"""
    candidates = await _loadCandidateFeatures(session, payload.data_object, payload.data_layer)
    system_prompt = _buildSystemPrompt(candidates)

    try:
        response = await llm_client.complete(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload.natural_language},
            ],
        )
    except Exception as e:
        logger.warning("parse-description LLM 调用失败: %s", e, exc_info=True)
        raise LLMUnavailableError("AI 辅助不可用，请手动填写 thresholds") from e

    content = getattr(response, "content", "") or ""
    try:
        parsed = json.loads(content)
        result = FeatureRuleParseDescriptionResponse.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning("parse-description LLM 输出解析失败: %s", e, exc_info=True)
        raise LLMUnavailableError(f"LLM 输出无法解析: {e}") from e

    # advisory read-only: 不写 outbox（event_type 语义要求 created/updated/deleted，
    # parse-description 是只读查询，不产生业务状态变更）
    return result
