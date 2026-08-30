"""AI 特征在线查询服务（Phase 4.4）。

Feature Layer 服务化的三块能力：
1. queryValues：按 feature_name + entity_keys + valid_at 直查 feature_value
   （供在线查询 API / 未来 Agent 消费，不经 LLM）。
2. buildFeatureCatalogText：渲染「可用 Feature 目录」文本，注入 NL2SQL 计划
   阶段 system prompt（与 dictionaryText 同模式：增强非依赖）。
3. matchPlanFeature：LLM 在计划 conditions/interpretation 中引用了某特征时
   识别出来，供 chat 流水线拦截（跳过 SQL 生成，直接回流特征值）。

护栏：只注入 ACTIVE + is_enabled 定义（上限 50 条）；feature_name 路径参数
走正则白名单；查询走 ORM 参数化。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import FeatureStatus
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import FeatureDefinition, FeatureValue
from app.domain.query_plan import UNANSWERABLE_TARGET, QueryPlan
from app.domain.schemas import FeatureValueRead
from app.services.messages_zh import (
    MSG_FEATURE_ENTITY_KEYS_TOO_MANY,
    MSG_FEATURE_NAME_INVALID,
    MSG_FEATURE_NOT_FOUND_BY_NAME,
)

logger = logging.getLogger(__name__)

# feature_name 命中约定：全大写 + 含下划线（特征名均为 SUPPLIER_XXX / FEATURE_XXX
# 形态；下划线约束排除 Q630 / OVER 这类普通大写词的误报）
FEATURE_NAME_RE = re.compile(r"\b([A-Z][A-Z0-9_]*_[A-Z0-9_]{1,99})\b")
# feature_name 路径参数白名单（1-100 字符）
FEATURE_PATH_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")

# prompt 注入目录的条目上限（防止几十条特征撑爆 prompt）
_CATALOG_MAX_ENTRIES = 50
# 目录单条中口径摘要（feature_definition）的截断长度
_DEFINITION_SUMMARY_MAX = 80
# 在线查询 entity_keys 上限
_MAX_ENTITY_KEYS = 100


def _validateFeatureName(name: str) -> str:
    """校验 feature_name 路径参数（白名单正则）；不合法抛 ValidationError -> 422。"""
    if not FEATURE_PATH_NAME_RE.match(name):
        raise ValidationError(MSG_FEATURE_NAME_INVALID.format(name=name))
    return name


def _matchFeatureName(texts: list[str]) -> str | None:
    """从文本列表中提取第一个符合 feature_name 命名约定的词。

    命中约定：全大写 + 至少一个下划线（特征名均为 SUPPLIER_XXX / FEATURE_XXX
    形态；排除 Q630、ID 这类普通大写词的误报）。多条文本按顺序取首个命中。
    """
    for text in texts:
        if not text:
            continue
        match = FEATURE_NAME_RE.search(text)
        if match:
            return match.group(1)
    return None


class FeatureQueryService:
    """Feature 在线查询 + prompt 目录渲染 + 计划特征匹配。"""

    # ------------------------------------------------------------------
    # 在线查询
    # ------------------------------------------------------------------

    async def queryValues(
        self,
        session: AsyncSession,
        feature_name: str,
        *,
        entity_keys: list[str] | None = None,
        valid_at: date | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按 feature_name 直查特征值，返回响应所需的结构化字段。

        - feature_name 白名单校验（422）/ 存在性校验（404）
        - entity_keys 超 100 个 -> 422
        - valid_at 缺省时取该特征最新窗口（MAX(valid_at)）
        返回 {feature, values, valid_at}（dict 而非 DTO：API 层组装响应）。
        """
        _validateFeatureName(feature_name)
        feature = await self._getByName(session, feature_name)
        if entity_keys is not None and len(entity_keys) > _MAX_ENTITY_KEYS:
            raise ValidationError(
                MSG_FEATURE_ENTITY_KEYS_TOO_MANY.format(limit=_MAX_ENTITY_KEYS)
            )

        effective_valid_at = valid_at
        if effective_valid_at is None:
            latest = await session.execute(
                select(FeatureValue.valid_at)
                .where(FeatureValue.feature_id == feature.id)
                .order_by(FeatureValue.valid_at.desc())
                .limit(1)
            )
            row = latest.scalar_one_or_none()
            if row is None:
                # 无任何值：返回空 values + 今日为有效期占位（调用方渲染空态）
                return {"feature": feature, "values": [], "valid_at": date.today()}
            effective_valid_at = row

        conditions = [FeatureValue.feature_id == feature.id, FeatureValue.valid_at == effective_valid_at]
        if entity_keys:
            conditions.append(FeatureValue.entity_key.in_(entity_keys))
        stmt = (
            select(FeatureValue)
            .where(*conditions)
            .order_by(FeatureValue.entity_key)
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        values = [FeatureValueRead.model_validate(v) for v in result.scalars().all()]
        return {"feature": feature, "values": values, "valid_at": effective_valid_at}

    async def _getByName(
        self, session: AsyncSession, feature_name: str
    ) -> FeatureDefinition:
        """按 feature_name 取定义；不存在抛 NotFoundError -> 404。"""
        result = await session.execute(
            select(FeatureDefinition).where(
                FeatureDefinition.feature_name == feature_name
            )
        )
        feature = result.scalar_one_or_none()
        if feature is None:
            raise NotFoundError(
                MSG_FEATURE_NOT_FOUND_BY_NAME.format(name=feature_name)
            )
        return feature

    # ------------------------------------------------------------------
    # prompt 目录注入
    # ------------------------------------------------------------------

    async def buildFeatureCatalogText(self, session: AsyncSession) -> str | None:
        """渲染可用 Feature 目录文本（仅 ACTIVE + enabled，上限 50 条）。

        空目录 / 加载异常 -> None（不注入，chat 流水线行为不变）；
        与 _loadDictionaryText 同降级策略。
        """
        try:
            features = await self._listCatalogFeatures(session)
        except Exception:
            logger.warning("Feature 目录加载失败，跳过注入", exc_info=True)
            return None
        return self._renderCatalogText(features)

    async def _listCatalogFeatures(
        self, session: AsyncSession
    ) -> list[FeatureDefinition]:
        stmt = (
            select(FeatureDefinition)
            .where(
                FeatureDefinition.is_enabled.is_(True),
                FeatureDefinition.status == FeatureStatus.ACTIVE,
            )
            .order_by(FeatureDefinition.feature_name)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    def _filterCatalogFeatures(
        self, features: list[FeatureDefinition]
    ) -> list[FeatureDefinition]:
        """过滤出可注入目录的定义（ACTIVE + enabled；调用方传入已过滤列表时幂等）。"""
        return [
            f
            for f in features
            if f.status == FeatureStatus.ACTIVE and f.is_enabled
        ]

    def _trimCatalog(
        self, features: list[FeatureDefinition]
    ) -> list[FeatureDefinition]:
        """目录条目上限截断（超限告警，不静默丢弃）。"""
        if len(features) <= _CATALOG_MAX_ENTRIES:
            return features
        logger.warning(
            "Feature 目录 %d 条超过上限 %d，仅注入前 %d 条",
            len(features),
            _CATALOG_MAX_ENTRIES,
            _CATALOG_MAX_ENTRIES,
        )
        return features[:_CATALOG_MAX_ENTRIES]

    def _renderCatalogText(
        self, features: list[FeatureDefinition]
    ) -> str | None:
        """渲染 <feature_catalog> 文本；空列表 -> None。"""
        eligible = self._trimCatalog(self._filterCatalogFeatures(features))
        if not eligible:
            return None
        entries = "\n".join(self._catalogEntry(f) for f in eligible)
        return (
            "以下预计算特征可直接引用（无需实时聚合计算）。若问题语义与某特征匹配，"
            "请在 conditions 中写明「使用特征 FEATURE_NAME」；否则按常规方式规划查询。\n"
            f"{entries}"
        )

    def _catalogEntry(self, feature: FeatureDefinition) -> str:
        """渲染目录单条：`- NAME | 别名: ... | 实体: ... | 窗口: ... | 单位: ... | 口径: ...`。"""
        parts = [f"- {feature.feature_name}"]
        if feature.feature_alias:
            parts.append(f"别名: {feature.feature_alias}")
        parts.append(f"实体: {feature.entity_type.value if hasattr(feature.entity_type, 'value') else feature.entity_type}")
        if feature.window_size:
            parts.append(f"窗口: {feature.window_size}")
        if feature.unit:
            parts.append(f"单位: {feature.unit}")
        if feature.feature_definition:
            summary = feature.feature_definition[:_DEFINITION_SUMMARY_MAX]
            if len(feature.feature_definition) > _DEFINITION_SUMMARY_MAX:
                summary += "..."
            parts.append(f"口径: {summary}")
        return " | ".join(parts)

    # ------------------------------------------------------------------
    # 计划特征匹配（chat 拦截路径）
    # ------------------------------------------------------------------

    def matchPlanFeature(
        self, plan: QueryPlan, features: list[FeatureDefinition]
    ) -> FeatureDefinition | None:
        """从查询计划提取 feature_name 并匹配可用特征目录；未引用返回 None。

        匹配顺序：conditions 优先（LLM 按 prompt 指引写「使用特征 X」），
        interpretation 兜底（LLM 复述理解时提及时也能命中）。
        """
        if plan.isUnanswerable or plan.target == UNANSWERABLE_TARGET:
            return None
        texts = list(plan.conditions) + (
            [plan.interpretation] if plan.interpretation else []
        )
        name = _matchFeatureName(texts)
        if name is None:
            return None
        return next(
            (f for f in features if f.feature_name == name and f.is_enabled),
            None,
        )

    # 测试别名（snake_case 契约保持，测试文件用私有名）
    _matchPlanFeatures = matchPlanFeature
