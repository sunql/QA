"""数据质量规则服务（Phase 1.1）。

仅承载规则定义本身的 CRUD；评估执行（Phase 1.2）与评分（Phase 1.3）由独立
service 实现，本服务不耦合业务库连接池，避免在本期引入 SQL 注入面。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataQualityRule
from app.domain.schemas import (
    DataQualityRuleCreate,
    DataQualityRuleRead,
    DataQualityRuleUpdate,
)
from app.services.messages_zh import MSG_DQ_RULE_CODE_EXISTS, MSG_DQ_RULE_NOT_FOUND


class DataQualityRuleService:
    """数据质量规则 CRUD。"""

    async def listRules(
        self,
        session: AsyncSession,
        *,
        ruleType: str | None = None,
        targetTable: str | None = None,
        enabledOnly: bool | None = None,
    ) -> list[DataQualityRule]:
        """列表查询，可按 ruleType / targetTable / enabledOnly 过滤。"""
        stmt = select(DataQualityRule).order_by(DataQualityRule.id)
        if ruleType is not None:
            stmt = stmt.where(DataQualityRule.rule_type == ruleType)
        if targetTable is not None:
            stmt = stmt.where(DataQualityRule.target_table == targetTable)
        if enabledOnly:
            stmt = stmt.where(DataQualityRule.is_enabled.is_(True))
        result = await session.execute(stmt)
        return list(result.scalars().all())

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
    ) -> DataQualityRule:
        """创建规则；rule_code 唯一冲突抛 ValidationError。"""
        existing = await session.execute(
            select(DataQualityRule).where(DataQualityRule.rule_code == dto.rule_code)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValidationError(MSG_DQ_RULE_CODE_EXISTS.format(code=dto.rule_code))
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
            owner=dto.owner,
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
    ) -> DataQualityRule:
        """局部更新规则；只覆盖非 None 字段。"""
        entity = await self.getRule(session, id)
        changes = dto.model_dump(exclude_unset=True, by_alias=False)
        for field, value in changes.items():
            setattr(entity, field, value)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def disableRule(self, session: AsyncSession, id: int) -> None:
        """软删除：is_enabled=false（保留规则用于审计与历史评估）。"""
        entity = await self.getRule(session, id)
        entity.is_enabled = False
        await session.commit()


def ruleToRead(rule: DataQualityRule) -> DataQualityRuleRead:
    """ORM -> Read DTO。集中导出便于路由层复用与单测覆盖。"""
    return DataQualityRuleRead.model_validate(rule, from_attributes=True)
