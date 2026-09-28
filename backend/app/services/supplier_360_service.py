"""供应商 360° ADS 视图服务（Phase 5.3）。

设计原则（plan §5.3）：
- **不建新表**，实时聚合 entity_mapping + feature_value + （未来）DW ADS view
- **异常隔离**：任意子模块失败 → log warning + 该字段空值返回，
  前端按字段渲染空态（如「暂无数据」占位）
- ACL：service 层不强制（API 层 `AclService.assertCanModify` 处理，
  保持 service 单元可测）
- 实体键映射：`enterprise_key` (BIGINT, MDM 主数据代理键 / SHA-256 8B hash) →
  `enterprise_code` (VARCHAR, 业务编码，如 THBI '10105' / 'SUP000001')；
  FeatureValue.entity_key 是 VARCHAR 业务编码（见 models.py FeatureDefinition
  注释 + SSOT §2）。

Phase 6.x：service 入口 `supplierKey` 接受 `str | int` —— 用户面对的"供应商编码"
是 VARCHAR（THBI BPSNUM_0 = '10105'），而 entity_mapping.enterprise_key 是
BIGINT 哈希（'10105' → 3823452429）。两个值都合法：
- 字符串输入：先按 enterprise_code 查（精确匹配业务码），回退 enterprise_key
- 整数输入：先按 enterprise_key 查（兼容旧路径），回退 enterprise_code
两条路径都返回 `(enterprise_key, enterprise_code)`，下游分别用于：
- entity_mapping JOIN → enterprise_key（BIGINT）
- feature_value JOIN → enterprise_code（VARCHAR）

Round 1 范围：仅接 PG entity_mapping + feature_value；DW ADS view / DW DWS 表
接入留 Round 2。
"""

from __future__ import annotations

import logging
from typing import Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import BusinessObjectCode, FeatureStatus
from app.domain.exceptions import NotFoundError
from app.domain.models import EntityMapping, FeatureDefinition, FeatureValue
from app.domain.schemas import (
    Supplier360EntityCode,
    Supplier360Kpi,
    Supplier360Profile,
    Supplier360Read,
)
from app.services.feature_rule_registry import feature_rule_registry
from app.services.messages_zh import MSG_SUPPLIER_360_NOT_FOUND

logger = logging.getLogger(__name__)


async def _kpiSlotFeatureNames(
    session: AsyncSession | None = None,
    data_object: str = "SUPPLIER",
    data_layer: str = "FEATURE",
) -> tuple[str, ...]:
    """聚合所有 enabled 规则的 feature_name（spec §6.4）。

    来源并集：
    1. FeatureRule registry（向后兼容，4 个内置风险规则仍生效）
    2. FeatureDefinition 表（entity_type + is_enabled + status=ACTIVE），
       仅在 session 非 None 时查询

    session 为 None 时退化为仅查 registry（保留单元测试路径）。
    """
    seen: set[str] = set()

    # 来源 1：FeatureRule registry（向后兼容）
    for target_level in ("RISK", "QUALITY_SCORE", "CUSTOM"):
        for rule in feature_rule_registry.getEnabledRules(
            data_object, data_layer, target_level
        ):
            seen.add(rule.feature_name)

    # 来源 2：FeatureDefinition 表（Option A — 仅 session 可用时查询）
    if session is not None:
        try:
            stmt = select(FeatureDefinition.feature_name).where(
                FeatureDefinition.entity_type == data_object,
                FeatureDefinition.is_enabled.is_(True),
                FeatureDefinition.status == FeatureStatus.ACTIVE,
            )
            result = await session.execute(stmt)
            for (fname,) in result.fetchall():
                seen.add(fname)
        except Exception:
            logger.warning(
                "_kpiSlotFeatureNames FeatureDefinition 查询失败 data_object=%s",
                data_object,
                exc_info=True,
            )

    return tuple(sorted(seen))


class Supplier360Service:
    """供应商 360° 视图聚合。

    get360(session, supplier_key) 是唯一对外入口：
    - 不存在 → NotFoundError（避免 typo 静默返回空对象）
    - 子模块失败 → log warning + 该字段空值，整体响应仍返回
    """

    async def get360(
        self, session: AsyncSession, supplierKey: Union[str, int]
    ) -> Supplier360Read:
        """实时聚合单供应商 360° 数据（plan §5.3）。

        supplierKey 同时接受 VARCHAR 业务码（如 THBI '10105'）与 BIGINT
        MDM 代理键（SHA-256 8B hash，如 3823452429）；详见模块 docstring。
        """
        enterprise_key, enterprise_code = await self._resolveSupplier(
            session, supplierKey
        )
        profile = Supplier360Profile(
            enterprise_key=enterprise_key,
            enterprise_code=enterprise_code,
            entity_type="SUPPLIER",
        )
        entity_codes = await self._safeLoadEntityCodes(session, enterprise_key)
        kpis = await self._safeLoadKpis(session, enterprise_code)
        return Supplier360Read(
            profile=profile, entity_codes=entity_codes, kpis=kpis
        )

    # ------------------------------------------------------------------
    # 内部：解析 supplierKey → (enterprise_key, enterprise_code)
    # ------------------------------------------------------------------

    async def _resolveSupplier(
        self, session: AsyncSession, supplierKey: Union[str, int]
    ) -> tuple[int, str]:
        """统一解析入口：返回 (enterprise_key, enterprise_code) 元组。

        策略：先按 enterprise_code 查（覆盖 THBI 业务码 / 旧合成 SUP 编码），
        未命中则按 enterprise_key 查（覆盖 BIGINT hash / 已存 chat 路径）。
        两条都失败 → NotFoundError。

        接受 str / int 入参；调用方不用关心类型（业务层永远是"业务码"或"代理键"）。
        """
        # Pass 1：按 enterprise_code 查（字符串输入主路径；整数转换后也可命中）
        try:
            row = (
                await session.execute(
                    select(
                        EntityMapping.enterprise_key,
                        EntityMapping.enterprise_code,
                    )
                    .where(
                        EntityMapping.entity_type == "SUPPLIER",
                        EntityMapping.enterprise_code == str(supplierKey),
                    )
                    .limit(1)
                )
            ).first()
        except Exception:
            logger.warning(
                "Supplier360 entity_mapping(enterprise_code) 查询失败 supplierKey=%s",
                supplierKey,
                exc_info=True,
            )
            row = None

        if row is not None:
            return int(row[0]), str(row[1])

        # Pass 2：按 enterprise_key 查（整数输入主路径；str 数字仅在 hash 巧合时命中）
        try:
            enterprise_key_int = int(supplierKey)
        except (TypeError, ValueError):
            enterprise_key_int = None
        if enterprise_key_int is not None:
            try:
                row2 = (
                    await session.execute(
                        select(
                            EntityMapping.enterprise_key,
                            EntityMapping.enterprise_code,
                        )
                        .where(
                            EntityMapping.entity_type == "SUPPLIER",
                            EntityMapping.enterprise_key == enterprise_key_int,
                        )
                        .limit(1)
                    )
                ).first()
            except Exception:
                logger.warning(
                    "Supplier360 entity_mapping(enterprise_key) 查询失败 supplierKey=%s",
                    supplierKey,
                    exc_info=True,
                )
                row2 = None
            if row2 is not None:
                return int(row2[0]), str(row2[1])

        # 两条都失败 → 404（防 typo 静默）
        raise NotFoundError(
            MSG_SUPPLIER_360_NOT_FOUND.format(key=supplierKey)
        )

    # ------------------------------------------------------------------
    # 内部：entity_codes（异常隔离）
    # ------------------------------------------------------------------

    async def _safeLoadEntityCodes(
        self, session: AsyncSession, enterpriseKey: int
    ) -> list[Supplier360EntityCode]:
        """加载该 supplier 的跨系统编码映射。失败 → 空列表 + WARN 日志。

        注：按 enterprise_key 查 —— entity_mapping 唯一键是
        (entity_type, enterprise_key, source_system)，不能用 enterprise_code 一次性
        命中所有 source_system。
        """
        try:
            result = await session.execute(
                select(EntityMapping).where(
                    EntityMapping.entity_type == "SUPPLIER",
                    EntityMapping.enterprise_key == enterpriseKey,
                )
            )
        except Exception:
            logger.warning(
                "Supplier360 entity_codes 加载失败 enterprise_key=%s",
                enterpriseKey,
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
        for feature_name in await _kpiSlotFeatureNames(session):
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
        """加载 _kpiSlotFeatureNames() 的全部 FeatureDefinition（不限 is_enabled / status）。

        调用方根据 is_enabled + status 决定渲染 / 占位 / 跳过；
        SQL 阶段不过滤是必要的，否则无法区分「DB 中无此行」与「DB 中存在但 disabled」。
        """
        stmt = select(FeatureDefinition).where(
            FeatureDefinition.feature_name.in_(
                await _kpiSlotFeatureNames(session)
            ),
            FeatureDefinition.entity_type == "SUPPLIER",
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
