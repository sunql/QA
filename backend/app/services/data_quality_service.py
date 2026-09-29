"""数据质量规则服务（Phase 1.1 + Phase 4.5 扩展 owner-based ACL）。

仅承载规则定义本身的 CRUD；评估执行（Phase 1.2）与评分（Phase 1.3）由独立
service 实现，本服务不耦合业务库连接池，避免在本期引入 SQL 注入面。

Phase 4.5 扩展：updateRule / disableRule 走 AclService.assertCanModify。
createRule 接收 actor 并将 owner 设为 actor.departments[0]，**不接受** client
body 中的 owner（DTO 已移除），防止越权声明 owner。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataQualityRule, DataSource, OntologyClass
from app.domain.schemas import (
    ClassOption,
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
        targetTables: list[str] | None = None,
        enabledOnly: bool | None = None,
        ruleName: str | None = None,
        datasourceId: int | None = None,
        severity: str | None = None,
        enabled: Literal["all", "enabled", "disabled"] | None = None,
        sourceClassId: int | None = None,
    ) -> list[DataQualityRule]:
        """列表查询；支持 8 字段过滤（feat-dq-rule-list-filters + dq-multi-select-batch-eval）。

        新增（dq-multi-select-batch-eval）：
        - targetTables: 多选目标表精确 IN 匹配（按已写入的 target_table 精确比对）；
          与 targetTable ILIKE 不能同时传——targetTables 优先。
        - sourceClassId: 本体类 id 精确（前端按 className 过滤的底层；ontology_class
          一对一对应 source_table，过滤类即过滤该类的所有规则）。

        其余字段语义保持：
        - ruleName: 规则名称 ILIKE 模糊
        - datasourceId: 数据源 id 精确
        - severity: 严重程度精确
        - enabled: 三态枚举 "all"/"enabled"/"disabled"
        - targetTable: 单值 ILIKE 模糊（兼容旧契约）
        - enabledOnly: bool 兼容旧调用方
        """
        stmt = select(DataQualityRule).order_by(DataQualityRule.id)
        if ruleType is not None:
            stmt = stmt.where(DataQualityRule.rule_type == ruleType)
        if targetTables:
            # 多选精确 IN 优先于单值 ILIKE
            stmt = stmt.where(DataQualityRule.target_table.in_(targetTables))
        elif targetTable is not None:
            stmt = stmt.where(DataQualityRule.target_table.ilike(f"%{targetTable}%"))
        if datasourceId is not None:
            stmt = stmt.where(DataQualityRule.datasource_id == datasourceId)
        if severity is not None:
            stmt = stmt.where(DataQualityRule.severity == severity)
        if ruleName is not None:
            stmt = stmt.where(DataQualityRule.rule_name.ilike(f"%{ruleName}%"))
        if sourceClassId is not None:
            stmt = stmt.where(DataQualityRule.source_class_id == sourceClassId)
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
        - classOptions：active 本体类全量（valid_to IS NULL；按 class_name 排序去重）
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
        # active 本体类全量 ORDER BY class_name
        classRows = await session.execute(
            select(OntologyClass.id, OntologyClass.class_name)
            .where(OntologyClass.valid_to.is_(None))
            .order_by(OntologyClass.class_name)
        )
        return RuleOptionsRead(
            rule_names=list(ruleNamesRows.scalars().all()),
            datasource_ids=[
                DatasourceOption(id=id_, name=name_) for id_, name_ in dsRows.all()
            ],
            target_tables=list(targetTablesRows.scalars().all()),
            severities=["HIGH", "MEDIUM", "LOW", "INFO"],
            class_options=[
                ClassOption(id=id_, class_name=name_) for id_, name_ in classRows.all()
            ],
        )

    async def nextRuleCode(
        self,
        session: AsyncSession,
        *,
        class_name: str,
        date_yyyymmdd: str | None = None,
    ) -> dict:
        """返回规则编码建议（feat-rule-batch-create）。

        编码格式：MU-DQ-{CLASS}-{YYYYMMDD}-{5位流水}
        - 类名清洗：保留 [A-Z0-9_]，超长截断到 12，纯特殊 → CLASS；
        - 同类同日的 MAX(seq)+1；空表返 1；
        - date_yyyymmdd 不传时默认今天（UTC）。

        注意：本函数只读 DB 给前端预览，**不锁定**。createRule() 时会按
        UniqueConstraint("rule_code") 兜底防并发冲突；冲突时前端重抓一次。
        """
        import re

        # 类名清洗（与前端 utils/ruleCodeGenerator.sanitizeClassName 保持一致）
        cls = (class_name or "").upper()
        cls = re.sub(r"[^A-Z0-9_]+", "_", cls)
        cls = cls.strip("_")
        if not cls:
            cls = "CLASS"
        cls = cls[:12]

        # 默认日期 = 今天 UTC
        if not date_yyyymmdd:
            date_yyyymmdd = datetime.utcnow().strftime("%Y%m%d")

        prefix = f"MU-DQ-{cls}-{date_yyyymmdd}-"
        # MAX(seq) from rule_code LIKE 'MU-DQ-CLS-YYYYMMDD-%'
        # 解析尾 5 位数字；LEFT(rule_code, ?) 不能跨方言，
        # 用 SUBSTRING 等价 + 显式 ::int 安全转换。
        stmt = (
            select(DataQualityRule.rule_code)
            .where(DataQualityRule.rule_code.like(f"{prefix}%"))
            .order_by(DataQualityRule.rule_code.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        next_seq = 1
        if row:
            tail = row[len(prefix):]
            if tail.isdigit():
                next_seq = int(tail) + 1
        seq_str = str(next_seq).zfill(5)
        return {
            "code": f"{prefix}{seq_str}",
            "seq": next_seq,
            "class_name": cls,
            "date": date_yyyymmdd,
        }

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
