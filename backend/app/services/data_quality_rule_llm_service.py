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
from sqlalchemy import or_, select
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
        "识别哪些属性适合建立约束（4 类）："
        "\n  - allowed_values 值域：description 明确列出枚举或可枚举值时"
        "\n  - not_null 非空：description 强调必填 / 必填 / 不能为空时"
        "\n  - range 区间：description 给出上下界 / 数值范围 / 日期范围时"
        "\n  - pattern 正则：description 给出格式样例 / 编码规则时"
        "\n\n本体类属性列表：\n"
        f"{props_block}\n\n"
        "输出 JSON：\n"
        '{"suggestions": ['
        '{"property_name": "...", '
        '"kind": "allowed_values|not_null|range|pattern", '
        '"values": ["val1", "val2"] | null, '
        '"min": "..." | null, "max": "..." | null, '
        '"pattern": "..." | null, '
        '"confidence": 0.0-1.0, "rationale": "..."}'
        "]}\n"
        "字段填充规则："
        "\n  - allowed_values：填 values（其它字段 null）"
        "\n  - not_null：values/min/max/pattern 全为 null"
        "\n  - range：填 min 和 max 字符串（数值/日期按 ISO 8601），values/pattern 为 null"
        "\n  - pattern：填 pattern 正则字符串，values/min/max 为 null"
        "\n仅从 description 文本推断，不要臆造值。"
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
                min_value=s.get("min"),
                max_value=s.get("max"),
                regex_pattern=s.get("pattern"),
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

    # 去重 + 丢弃未映射项：
    # LLM 抖动时常输出同 (property_id, kind) 重复条目；同一 property 不同 kind 是
    # 合法多约束（如 not_null + allowed_values），保留。
    # property_id=0 是 LLM 输出未在本体 prop_map 中命中的 property_name，
    # 丢弃避免前端拿占位 0 当真实 id。
    # 关键顺序：先按 (property_id, kind) 去重，后丢弃 property_id=0。
    # （先去重再丢 0，避免 0+kind 互相覆盖误丢合法未映射对。）
    deduped: list[PropertyConstraintSuggestionRead] = []
    seen: set[tuple[int, str]] = set()
    for s in suggestions:
        key = (s.property_id, s.kind)
        if key in seen:
            continue
        seen.add(key)
        if s.property_id == 0:
            # LLM 引用了不存在的属性，丢弃（前端无法渲染有效约束）。
            continue
        deduped.append(s)
    suggestions = deduped

    # 查询当前类下「已沉淀任一约束」的 propertyId 列表，
    # 供前端初始化 adoptedIds（刷新页面也保持已采纳状态）。
    # 任一约束字段非空都视为已采纳，避免用户重复点击。
    # 关键变更：原只查 allowed_values；现 OR 5 列（feat-ontology-property-constraints）。
    persisted_result = await session.execute(
        select(OntologyProperty.id).where(
            OntologyProperty.class_id == payload.class_id,
            or_(
                OntologyProperty.allowed_values.isnot(None),
                OntologyProperty.is_not_null.is_(True),
                OntologyProperty.min_value.isnot(None),
                OntologyProperty.max_value.isnot(None),
                OntologyProperty.regex_pattern.isnot(None),
            ),
        )
    )
    persisted_property_ids = list(persisted_result.scalars().all())

    return ParseDescriptionsResponse(
        suggestions=suggestions,
        persisted_property_ids=persisted_property_ids,
    )
