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
import re
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
from app.infrastructure.llm.base_client import LlmMessage
from app.services.messages_zh import (
    MSG_DQ_GEN_CLASS_NOT_FOUND,
    MSG_DQ_GEN_LLM_PARSE_ERROR,
    MSG_DQ_GEN_LLM_UNAVAILABLE,
)

logger = logging.getLogger(__name__)


# 匹配 ```json ... ``` 或 ``` ... ``` 代码块（含可选 json 语言标记）；
# re.DOTALL 跨行匹配；re.IGNORECASE 兼容 ```JSON``` 大小写。
# 真实复现：deepseek-chat 返回的 LLM content 普遍被此 fence 包裹，
# 之前裸 json.loads 失败 → JSONDecodeError → 503 LLMUnavailableError(MSG_DQ_GEN_LLM_PARSE_ERROR)。
# 与 nl2sql_service._JSON_FENCE_RE 同语义，本服务独立一份避免跨服务耦合。
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _stripJsonFence(content: str) -> str:
    """从 LLM 回复中抽出 JSON 文本：优先 ```json/``` 代码块，否则原样返回。

    返回的字符串不保证可被 json.loads 解析（仍可能不是 JSON）；仅负责剥掉 fence。
    """
    if not content:
        return content
    match = _JSON_FENCE_RE.search(content)
    if match:
        return match.group(1).strip()
    return content.strip()


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
                LlmMessage(role="system", content=system_prompt),
                LlmMessage(
                    role="user",
                    content="请分析以上本体类属性，识别候选约束。",
                ),
            ],
        )
    except Exception as e:
        logger.warning("parse-descriptions LLM 调用失败: %s", e, exc_info=True)
        raise LLMUnavailableError(MSG_DQ_GEN_LLM_UNAVAILABLE) from e

    content = getattr(response, "content", "") or ""
    # LLM 几乎都会用 ```json ... ``` 包裹 JSON 输出；
    # 之前裸 json.loads 失败 → JSONDecodeError → 503 LLMUnavailableError(MSG_DQ_GEN_LLM_PARSE_ERROR)。
    # _stripJsonFence 优先剥 ```json/``` fence，否则原样；
    # 仍非 JSON 时由下层 json.JSONDecodeError 兜底（保留 503 契约）。
    payload_text = _stripJsonFence(content)
    try:
        parsed = json.loads(payload_text)
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

    # 查询当前类下已沉淀 allowed_values 的 propertyId 列表，
    # 供前端初始化 adoptedIds（刷新页面也保持已采纳状态）。
    persisted_result = await session.execute(
        select(OntologyProperty.id).where(
            OntologyProperty.class_id == payload.class_id,
            OntologyProperty.allowed_values.isnot(None),
        )
    )
    persisted_property_ids = list(persisted_result.scalars().all())

    return ParseDescriptionsResponse(
        suggestions=suggestions,
        persisted_property_ids=persisted_property_ids,
    )
