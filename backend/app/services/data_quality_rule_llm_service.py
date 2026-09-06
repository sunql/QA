"""parse-descriptions LLM 服务（dq-rule-auto-generation Task 6）。

给定本体类，加载所有属性的 description → system prompt 注入 →
LLM 返回 Pydantic 结构化输出 → 校验通过返回；
任意异常 → LLMUnavailableError（503）。

advisory 只读，不写 outbox（event_type 语义要求 created/updated/deleted，
parse-description 是只读查询，不产生业务状态变更）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import LLMUnavailableError, NotFoundError
from app.domain.models import OntologyClass, OntologyProperty
from app.domain.schemas import (
    ParseDescriptionsRequest,
    ParseDescriptionsResponse,
    PropertyConstraintSuggestionRead,
)
from app.services.messages_zh import (
    MSG_DQ_GEN_CLASS_NOT_FOUND,
    MSG_DQ_GEN_LLM_PARSE_ERROR,
    MSG_DQ_GEN_LLM_UNAVAILABLE,
)

logger = logging.getLogger(__name__)


async def _loadClassProperties(
    session: AsyncSession, class_id: int,
) -> tuple[OntologyClass, list[OntologyProperty]]:
    """加载本体类及其所有属性，不存在 → 404。"""
    cls = await session.get(OntologyClass, class_id)
    if cls is None:
        raise NotFoundError(MSG_DQ_GEN_CLASS_NOT_FOUND.format(id=class_id))

    props = (
        await session.execute(
            select(OntologyProperty).where(OntologyProperty.class_id == class_id)
        )
    ).scalars().all()
    return cls, list(props)


def _buildSystemPrompt(
    class_name: str, properties: list[OntologyProperty],
) -> str:
    """构建 system prompt：逐属性列 property_name/data_type/description。"""
    prop_lines = []
    for p in properties:
        desc = p.description or ""
        prop_lines.append(
            f"- {p.property_name}（{p.data_type}）{desc}"
        )
    props_block = "\n".join(prop_lines) or "(该类暂无属性描述)"
    return (
        "你是数据质量规则配置助理。基于本体类属性的 description 字段，"
        "识别哪些属性适合建立值域约束（allowed_values）或非空约束（not_null）。"
        "\n\n本体类属性列表：\n"
        f"{props_block}\n\n"
        "输出 JSON：\n"
        '{"suggestions": ['
        '{"property_name": "...", "kind": "allowed_values|not_null", '
        '"values": ["val1", "val2"] | null, "confidence": 0.0-1.0, "rationale": "..."}'
        "]}\n"
        "kind=not_null 时 values=null。仅从 description 文本推断，不要臆造值。"
    )


async def parsePropertyDescriptions(
    session: AsyncSession,
    payload: ParseDescriptionsRequest,
    llm_client: Any,
    actor: str | None = None,
) -> ParseDescriptionsResponse:
    """调 LLM 从属性描述中提取候选约束建议；失败 → 503。"""
    cls, props = await _loadClassProperties(session, payload.class_id)
    system_prompt = _buildSystemPrompt(cls.class_name, props)

    try:
        response = await llm_client.complete(
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": "请分析以上本体类属性，识别候选约束。",
                },
            ],
        )
    except Exception as e:
        logger.warning("parse-descriptions LLM 调用失败: %s", e, exc_info=True)
        raise LLMUnavailableError(MSG_DQ_GEN_LLM_UNAVAILABLE) from e

    content = getattr(response, "content", "") or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning("parse-descriptions LLM 输出解析失败: %s", e, exc_info=True)
        raise LLMUnavailableError(MSG_DQ_GEN_LLM_PARSE_ERROR) from e

    try:
        suggestions = [
            PropertyConstraintSuggestionRead(
                property_id=0,  # 占位，caller 负责映射
                property_name=s["property_name"],
                kind=s["kind"],
                values=s.get("values"),
                confidence=float(s["confidence"]),
                rationale=s["rationale"],
            )
            for s in parsed.get("suggestions", [])
        ]
    except (KeyError, TypeError, ValidationError) as e:
        logger.warning("parse-descriptions 输出结构校验失败: %s", e, exc_info=True)
        raise LLMUnavailableError(MSG_DQ_GEN_LLM_PARSE_ERROR) from e

    # 回填 property_id（按 property_name 匹配）
    prop_map = {p.property_name.upper(): p.id for p in props}
    for s in suggestions:
        s.property_id = prop_map.get(s.property_name.upper(), 0)

    return ParseDescriptionsResponse(suggestions=suggestions)
