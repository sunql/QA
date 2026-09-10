"""数据质量规则服务（Phase 1.1 + Phase 4.5 扩展 owner-based ACL）。

仅承载规则定义本身的 CRUD；评估执行（Phase 1.2）与评分（Phase 1.3）由独立
service 实现，本服务不耦合业务库连接池，避免在本期引入 SQL 注入面。

Phase 4.5 扩展：updateRule / disableRule 走 AclService.assertCanModify。
createRule 接收 actor 并将 owner 设为 actor.departments[0]，**不接受** client
body 中的 owner（DTO 已移除），防止越权声明 owner。
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataQualityRule, DataSource
from app.domain.schemas import (
    DataQualityRuleCreate,
    DataQualityRuleRead,
    DataQualityRuleUpdate,
    DatasourceOption,
    RuleOptionsRead,
)
from app.services.acl_service import AclService
from app.services.messages_zh import MSG_DQ_RULE_CODE_EXISTS, MSG_DQ_RULE_NOT_FOUND


class DataQualityRuleService:
    """数据质量规则 CRUD。"""

    def __init__(self, acl: AclService | None = None) -> None:
        # 默认实例：service 内部 new；测试可注入 mock
        self._acl = acl or AclService()

    async def listRules(
        self,
        session: AsyncSession,
        *,
        ruleType: str | None = None,
        targetTable: str | None = None,
        enabledOnly: bool | None = None,
        ruleName: str | None = None,
        datasourceId: int | None = None,
        severity: str | None = None,
        enabled: Literal["all", "enabled", "disabled"] | None = None,
    ) -> list[DataQualityRule]:
        """列表查询；支持 6 字段过滤（feat-dq-rule-list-filters）。

        新增：
        - ruleName: 规则名称 ILIKE 模糊（用户在前端下拉里 showSearch 输入关键字）
        - datasourceId: 数据源 id 精确
        - severity: 严重程度精确（HIGH/MEDIUM/LOW/INFO）
        - enabled: 三态枚举 "all"/"enabled"/"disabled"，替代旧 enabledOnly bool

        targetTable 改为 ILIKE 模糊（兼容旧契约的同时补"PO" → PO_HEADER 语义）。
        enabledOnly 保留兼容路径（feat 前端若仍在用）：bool true 视为 enabledOnly。
        """
        stmt = select(DataQualityRule).order_by(DataQualityRule.id)
        if ruleType is not None:
            stmt = stmt.where(DataQualityRule.rule_type == ruleType)
        if targetTable is not None:
            stmt = stmt.where(DataQualityRule.target_table.ilike(f"%{targetTable}%"))
        if datasourceId is not None:
            stmt = stmt.where(DataQualityRule.datasource_id == datasourceId)
        if severity is not None:
            stmt = stmt.where(DataQualityRule.severity == severity)
        if ruleName is not None:
            stmt = stmt.where(DataQualityRule.rule_name.ilike(f"%{ruleName}%"))
        # enabled 三态优先（feat 后）；enabledOnly bool 兼容旧调用方
        if enabled == "enabled":
            stmt = stmt.where(DataQualityRule.is_enabled.is_(True))
        elif enabled == "disabled":
            stmt = stmt.where(DataQualityRule.is_enabled.is_(False))
        elif enabledOnly:
            stmt = stmt.where(DataQualityRule.is_enabled.is_(True))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def listOptions(self, session: AsyncSession) -> RuleOptionsRead:
        """返回规则列表筛选下拉的全部可选值（feat-dq-rule-list-filters）。

        - ruleNames / targetTables：DISTINCT 自 data_quality_rule 已写入的列
          （业务方手工 + 自动生成的混合体，下拉从真实使用过的值里选最贴合）
        - datasourceIds：active 数据源全量（data_source.is_active=true）
        - severities：静态全集（data_quality_rule.severity 枚举值不动态变化）
        """
        # DISTINCT rule_name ORDER BY 1（空值跳过——存量数据可能有 NULL）
        ruleNamesRows = await session.execute(
            select(DataQualityRule.rule_name)
            .where(DataQualityRule.rule_name.isnot(None))
            .distinct()
            .order_by(DataQualityRule.rule_name)
        )
        # DISTINCT target_table ORDER BY 1
        targetTablesRows = await session.execute(
            select(DataQualityRule.target_table)
            .where(DataQualityRule.target_table.isnot(None))
            .distinct()
            .order_by(DataQualityRule.target_table)
        )
        # active 数据源全量 ORDER BY name
        dsRows = await session.execute(
            select(DataSource.id, DataSource.name)
            .where(DataSource.is_active.is_(True))
            .order_by(DataSource.name)
        )
        return RuleOptionsRead(
            rule_names=list(ruleNamesRows.scalars().all()),
            datasource_ids=[
                DatasourceOption(id=id_, name=name_) for id_, name_ in dsRows.all()
            ],
            target_tables=list(targetTablesRows.scalars().all()),
            severities=["HIGH", "MEDIUM", "LOW", "INFO"],
        )

    async def getRule(self, session: AsyncSession, id: int) -> DataQualityRule:
        """按 id 取规则；不存在抛 NotFoundError。"""
        entity = await session.get(DataQualityRule, id)
        if entity is None:
            raise NotFoundError(MSG_DQ_RULE_NOT_FOUND.format(id=id))
        return entity

    async def createRule(
        self,
        session: AsyncSession,
        dto: DataQualityRuleCreate,
        actor: CurrentUser,
    ) -> DataQualityRule:
        """创建规则；rule_code 唯一冲突抛 ValidationError。

        Phase 4.5：owner 由 actor.departments[0] 派生，**不接受** client body
        中的 owner（DTO 已移除），防止越权。actor.departments 为空 → owner=None
        → 仅 admin 可改。
        """
        existing = await session.execute(
            select(DataQualityRule).where(DataQualityRule.rule_code == dto.rule_code)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValidationError(MSG_DQ_RULE_CODE_EXISTS.format(code=dto.rule_code))
        derivedOwner = actor.departments[0] if actor.departments else None
        entity = DataQualityRule(
            rule_name=dto.rule_name,
            rule_code=dto.rule_code,
            datasource_id=dto.datasource_id,
            target_table=dto.target_table,
            target_column=dto.target_column,
            rule_type=dto.rule_type,
            rule_expression=dto.rule_expression,
            threshold=dto.threshold,
            severity=dto.severity,
            is_enabled=dto.is_enabled,
            version=dto.version,
            owner=derivedOwner,
            description=dto.description,
        )
        session.add(entity)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def updateRule(
        self,
        session: AsyncSession,
        id: int,
        dto: DataQualityRuleUpdate,
        actor: CurrentUser,
    ) -> DataQualityRule:
        """局部更新规则；只覆盖非 None 字段。

        Phase 4.5：先 ACL 检查（owner 不匹配 + 非 admin → 403），
        再 apply dto changes + commit。
        """
        entity = await self.getRule(session, id)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="DATA_QUALITY_RULE",
            entity_code=entity.rule_code,
        )
        changes = dto.model_dump(exclude_unset=True, by_alias=False)
        for field, value in changes.items():
            setattr(entity, field, value)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def disableRule(
        self,
        session: AsyncSession,
        id: int,
        actor: CurrentUser,
    ) -> None:
        """软删除：is_enabled=false（保留规则用于审计与历史评估）。

        Phase 4.5：先 ACL 检查（owner 不匹配 + 非 admin → 403）。
        """
        entity = await self.getRule(session, id)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="DATA_QUALITY_RULE",
            entity_code=entity.rule_code,
        )
        entity.is_enabled = False
        await session.commit()


def ruleToRead(rule: DataQualityRule) -> DataQualityRuleRead:
    """ORM -> Read DTO。集中导出便于路由层复用与单测覆盖。"""
    return DataQualityRuleRead.model_validate(rule, from_attributes=True)
