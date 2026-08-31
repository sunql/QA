"""供应商 360° ADS 视图服务（Phase 5.3）。

设计原则（plan §5.3）：
- **不建新表**，实时聚合 entity_mapping + feature_value + （未来）DW ADS view
- **异常隔离**：任意子模块失败 → log warning + 该字段空值返回，
  前端按字段渲染空态（如「暂无数据」占位）
- ACL：service 层不强制（API 层 `AclService.assertCanModify` 处理，
  保持 service 单元可测）
- 实体键映射：`enterprise_key` (BIGINT, MDM 主数据代理键) → `enterprise_code`
  (VARCHAR, 业务编码，如 SUP000001)；FeatureValue.entity_key 是 VARCHAR
  业务编码（见 models.py FeatureDefinition 注释 + SSOT §2）

Round 1 范围：仅接 PG entity_mapping + feature_value；DW ADS view / DW DWS 表
接入留 Round 2。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EntityType, FeatureStatus
from app.domain.exceptions import NotFoundError
from app.domain.models import EntityMapping, FeatureDefinition, FeatureValue
from app.domain.schemas import (
    Supplier360EntityCode,
    Supplier360Kpi,
    Supplier360Profile,
    Supplier360Read,
)
from app.services.messages_zh import MSG_SUPPLIER_360_NOT_FOUND

logger = logging.getLogger(__name__)

# Phase 5.3 范围内 4 个 SUPPLIER 特征（与 seed_features.py 对齐）。
# 顺序在前端展示上有意义（按"交付→质量→价格→风险"逻辑分组）。
DEFAULT_SUPPLIER_FEATURES: tuple[str, ...] = (
    "SUPPLIER_OTD_3M",
    "SUPPLIER_DEFECT_RATE_3M",
    "SUPPLIER_PRICE_VARIANCE_3M",
    "SUPPLIER_RISK_SCORE",
)


class Supplier360Service:
    """供应商 360° 视图聚合。

    get360(session, supplier_key) 是唯一对外入口：
    - 不存在 → NotFoundError（避免 typo 静默返回空对象）
    - 子模块失败 → log warning + 该字段空值，整体响应仍返回
    """

    async def get360(
        self, session: AsyncSession, supplierKey: int
    ) -> Supplier360Read:
        """实时聚合单供应商 360° 数据（plan §5.3）。"""
        enterprise_code = await self._loadEnterpriseCodeOr404(
            session, supplierKey
        )
        profile = Supplier360Profile(
            enterprise_key=supplierKey,
            enterprise_code=enterprise_code,
            entity_type=EntityType.SUPPLIER,
        )
        entity_codes = await self._safeLoadEntityCodes(session, supplierKey)
        kpis = await self._safeLoadKpis(session, enterprise_code)
        return Supplier360Read(
            profile=profile, entity_codes=entity_codes, kpis=kpis
        )

    # ------------------------------------------------------------------
    # 内部：enterprise_code 查表（找不到 → 404）
    # ------------------------------------------------------------------

    async def _loadEnterpriseCodeOr404(
        self, session: AsyncSession, supplierKey: int
    ) -> str:
        """按 enterprise_key 取任一映射行的 enterprise_code（任意源系统都行）。

        同一 enterprise_key 在不同 source_system 下 enterprise_code 一致（MDM
        主数据原则）；故只取一条即可。找不到 → 抛 NotFoundError。
        """
        try:
            result = await session.execute(
                select(EntityMapping.enterprise_code)
                .where(
                    EntityMapping.entity_type == EntityType.SUPPLIER,
                    EntityMapping.enterprise_key == supplierKey,
                )
                .limit(1)
            )
        except Exception:
            logger.warning(
                "Supplier360 entity_mapping 查询失败 supplier_key=%s",
                supplierKey,
                exc_info=True,
            )
            raise NotFoundError(
                MSG_SUPPLIER_360_NOT_FOUND.format(key=supplierKey)
            )

        code = result.scalar_one_or_none()
        if code is None:
            raise NotFoundError(
                MSG_SUPPLIER_360_NOT_FOUND.format(key=supplierKey)
            )
        return code

    # ------------------------------------------------------------------
    # 内部：entity_codes（异常隔离）
    # ------------------------------------------------------------------

    async def _safeLoadEntityCodes(
        self, session: AsyncSession, supplierKey: int
    ) -> list[Supplier360EntityCode]:
        """加载该 supplier 的跨系统编码映射。失败 → 空列表 + WARN 日志。"""
        try:
            result = await session.execute(
                select(EntityMapping).where(
                    EntityMapping.entity_type == EntityType.SUPPLIER,
                    EntityMapping.enterprise_key == supplierKey,
                )
            )
        except Exception:
            logger.warning(
                "Supplier360 entity_codes 加载失败 supplier_key=%s",
                supplierKey,
                exc_info=True,
            )
            return []

        # 注：SourceSystem / MatchRule 是 (str, Enum)，SQLAlchemy 在 ORM 行上
        # 返回 str 本身（而非 Enum 实例），故用 hasattr(., 'value') 兜底：
        # str 没有 .value，Enum 有；与 feature_query_service.py 同模式。
        return [
            Supplier360EntityCode(
                source_system=(
                    row.source_system.value
                    if hasattr(row.source_system, "value")
                    else row.source_system
                ),
                source_code=row.source_code,
                source_key=row.source_key,
                match_rule=(
                    row.match_rule.value
                    if hasattr(row.match_rule, "value")
                    else row.match_rule
                ),
            )
            for row in result.scalars().all()
        ]

    # ------------------------------------------------------------------
    # 内部：kpis（异常隔离 + 占位逻辑）
    # ------------------------------------------------------------------

    async def _safeLoadKpis(
        self, session: AsyncSession, enterpriseCode: str
    ) -> list[Supplier360Kpi]:
        """加载 4 个 SUPPLIER 特征的最新值。

        行为契约（与单测对齐）：
        - DB 中存在 + is_enabled + status=ACTIVE → 渲染（latest 由是否取到值决定）
        - DB 中存在但 disabled / DRAFT → 跳过（前端无意义渲染）
        - DB 中不存在 → 渲染占位（feature_name + latest=False）
        - 整体查询失败 → 返回 4 个占位（latest=False），不抛
        """
        try:
            all_defs = await self._loadAllDefaultDefinitions(session)
        except Exception:
            logger.warning(
                "Supplier360 feature_definition 加载失败 enterprise_code=%s",
                enterpriseCode,
                exc_info=True,
            )
            all_defs = {}

        result: list[Supplier360Kpi] = []
        for feature_name in DEFAULT_SUPPLIER_FEATURES:
            fd = all_defs.get(feature_name)
            if fd is None:
                # DB 中完全无此 feature → 占位
                result.append(
                    Supplier360Kpi(feature_name=feature_name, latest=False)
                )
                continue
            if not fd.is_enabled or fd.status != FeatureStatus.ACTIVE:
                # 存在但未启用 / 草稿 → 跳过（前端无意义渲染）
                continue
            try:
                fv = await self._loadLatestFeatureValue(
                    session, fd.id, enterpriseCode
                )
            except Exception:
                logger.warning(
                    "Supplier360 feature_value 取最新失败 feature=%s entity=%s",
                    feature_name,
                    enterpriseCode,
                    exc_info=True,
                )
                fv = None
            result.append(_buildKpi(fd, fv))
        return result

    async def _loadAllDefaultDefinitions(
        self, session: AsyncSession
    ) -> dict[str, FeatureDefinition]:
        """加载 DEFAULT_SUPPLIER_FEATURES 的全部 FeatureDefinition（不限 is_enabled / status）。

        调用方根据 is_enabled + status 决定渲染 / 占位 / 跳过；
        SQL 阶段不过滤是必要的，否则无法区分「DB 中无此行」与「DB 中存在但 disabled」。
        """
        stmt = select(FeatureDefinition).where(
            FeatureDefinition.feature_name.in_(DEFAULT_SUPPLIER_FEATURES),
            FeatureDefinition.entity_type == EntityType.SUPPLIER,
        )
        result = await session.execute(stmt)
        return {fd.feature_name: fd for fd in result.scalars().all()}

    async def _loadLatestFeatureValue(
        self, session: AsyncSession, featureId: int, entityKey: str
    ) -> FeatureValue | None:
        """按 (feature_id, entity_key) 取最新一行：valid_at DESC, computed_at DESC。"""
        stmt = (
            select(FeatureValue)
            .where(
                FeatureValue.feature_id == featureId,
                FeatureValue.entity_key == entityKey,
            )
            .order_by(FeatureValue.valid_at.desc(), FeatureValue.computed_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


def _buildKpi(
    fd: FeatureDefinition, fv: FeatureValue | None
) -> Supplier360Kpi:
    """把 (FeatureDefinition, FeatureValue|None) 组装成 Supplier360Kpi。

    取到值 → latest=True；否则 latest=False（前端展示「暂无数据」占位）。
    """
    if fv is None:
        return Supplier360Kpi(
            feature_name=fd.feature_name,
            feature_alias=fd.feature_alias,
            unit=fd.unit,
            latest=False,
        )
    return Supplier360Kpi(
        feature_name=fd.feature_name,
        feature_alias=fd.feature_alias,
        value=fv.value,
        value_text=fv.value_text,
        unit=fd.unit,
        valid_at=fv.valid_at,
        computed_at=fv.computed_at,
        latest=True,
    )
